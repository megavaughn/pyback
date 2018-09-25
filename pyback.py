#!/usr/bin/env python3

##############################
# rsync via ssh backup script
# Anthony Vaughn
#
# v0.1.0
##############################

import argparse
from email.message import EmailMessage
import json
import os
import re
import smtplib
import socket
import subprocess
import sys
import time

class BackupTarget:
    def __init__(self, address, user, port, key):
        self.address = address
        self.key = key
        self.port = port
        self.user = user

class BackupSet:
    def __init__(self, localdir, remotedir, excludes, retention):
        self.localdir = localdir
        self.remotedir = remotedir
        self.excludes = excludes
        self.retention = retention

class BackupNotifier:
    def __init__(self, server, port, user, password, fromaddr, toaddr):
        self._server = server
        self._port = port
        self._user = user
        self._password = password
        self._fromaddr = fromaddr
        self._toaddr = toaddr
        self._success_messages = []
        self._error_messages = []
        self._warning_messages = []
    
    def _send_email_notification(self, subject, message):
        emailMsg = EmailMessage()
        emailMsg.set_content(message)
        emailMsg['Subject'] = subject
        emailMsg['From'] = self._fromaddr
        emailMsg['To'] = self._toaddr
        
        SMTPserver = smtplib.SMTP_SSL(self._server, self._port)
        SMTPserver.login(self._user, self._password)
        SMTPserver.send_message(emailMsg)
        SMTPserver.quit()   
    
    def add_success(self, message):
        self._success_messages.append(message)
        
    def add_error(self, message, e=None):
        body = message + "\n"
        if e:
            body += "Call:\n " + ' '.join(e.cmd) + "\n\n" + "Call exited with status " + str(e.returncode) + "\n\n" + "Stdout:\n" + e.stdout.decode('utf8') + "\n\n" + "Stderr:\n" + e.stderr.decode('utf8') + "\n\n" + "Output:\n" + e.output.decode('utf8') + "\n\n"
        self._error_messages.append(body)
        
    def add_warning(self, message, e=None):
        body = message + "\n"
        if e:
            body += "Call:\n " + ' '.join(e.cmd) + "\n\n" + "Call exited with status " + str(e.returncode) + "\n\n" + "Stdout:\n " + str(e.stdout) + "\n\n" + "Stderr:\n " + str(e.stderr) + "\n\n" + "Output:\n " + str(e.output) + "\n\n"
        self._warning_messages.append(body)
        
    def send_notification(self):
        hostname = socket.gethostname()

        num_successes = len(self._success_messages)
        num_errors = len(self._error_messages)
        num_warnings = len(self._warning_messages)

        subject = hostname + " Backup Complete: " + str(num_errors) + " Errors, " + str(num_successes) + " Successes, " + str(num_warnings) + " Warnings."

        body=""
        if num_errors:
            for error in self._error_messages:
                body += "--- ERROR:\n" + error + "\n\n"

        if num_warnings:
            for warning in self._warning_messages:
                body += "--- WARNING:\n" + warning + "\n\n"

        if num_successes:
            for success in self._success_messages:
                body += "--- OK:\n" + success + "\n\n"
        
        self._send_email_notification(subject, body)


def main():
    if len(sys.argv) != 2:
        print ("main: Invalid number of arguments.\nUsage: backup_computer <JSON settings file>")
        sys.exit()
    
    settingsFile = sys.argv[1]
    
    settingsData = {}
    with open(settingsFile) as f:
        settingsData = json.load(f)
    
    sshexe = "ssh"
    if "ssh" in settingsData:
        sshexe = settingsData["ssh"]
 
    rsyncexe = "rsync"
    if "rsync" in settingsData:
        rsyncexe = settingsData["rsync"]
    
    backupBaseExcludes = []
    if "excludes" in settingsData:
        for exclude in settingsData["excludes"]:
            backupBaseExcludes.append("--exclude="+exclude["exclude"])
    
    backupNotifier = BackupNotifier(settingsData["smtpserver"], settingsData["smtpport"], settingsData["smtpuser"], settingsData["smtppassword"], settingsData["smtpfrom"], settingsData["smtpto"])
    backupTargets = [BackupTarget(server["address"], server["user"], server["port"], server["key"]) for server in settingsData["backuptargets"]]
    
    backupSets = []
    for backupset in settingsData["backupsets"]:
        backupsetLocalDir = backupset["localdir"]
        if not (os.path.isdir(backupsetLocalDir)):
            backupNotifier.add_warning("Local Backup Directory Missing", "Local directory to backup, " + backupsetLocalDir + " does not exist. Should it? Backup set skipped.")
            continue
        backupsetRemoteDir = backupset["remotedir"]
        backupsetExcludes = []
        if "excludes" in backupset:
            for exclude in backupset["excludes"]:
                backupsetExcludes.append("--exclude="+exclude["exclude"])
        
        retentionMinutes = 12 * 52 * 7 * 24 * 60
        if "retention" in backupset:
            retentionData = backupset["retention"]
            retentionTime, retentionUnit = int(retentionData[:-1]), str(retentionData[-1:]).lower()
            if 'y' in retentionUnit:
                retentionMinutes = retentionTime * 365 * 24 * 60
            elif 'w' in retentionUnit:
                retentionMinutes = retentionTime * 52 * 7 * 24 * 60
            elif 'd' in retentionUnit:
                retentionMinutes = retentionTime * 24 * 60
            elif 'h' in retentionUnit:
                retentionMinutes = retentionTime * 60
            elif 'm' in retentionUnit:
                retentionMinutes = retentionTime
            else:
                backupNotifier.add_warning("No Retention Policy Set on Backup Set", "Retention for " + backupsetLocalDir + " defaulting to 12 weeks.")

        backupSets.append(BackupSet(backupsetLocalDir, backupsetRemoteDir, backupBaseExcludes+backupsetExcludes, retentionMinutes))
        
    for backupTarget in backupTargets:
        sshparams = ["-i", backupTarget.key, "-p", backupTarget.port, backupTarget.user + "@" + backupTarget.address]
        sshbasecmd = [sshexe] + sshparams
        for backupSet in backupSets:
            remoteBaseDir = backupSet.remotedir
            remoteTmpDir = remoteBaseDir + "/" + ".tmp"
            escapedRemoteBaseDir = re.escape(remoteBaseDir)
            escapedRemoteTmpDir = re.escape(remoteTmpDir)
            
            # Could be first run, create the base directory if it doesn't exist
            try:
                subprocess.run(sshbasecmd + ['mkdir','-p',escapedRemoteBaseDir], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as e:
                backupNotifier.add_error("Could not create remote directory:\n" + escapedRemoteBaseDir+"\n\n" +"Backup set skipped.", e)
                continue
            
            # Remove tmp dir if it exists (i.e. from a previously failed backup)
            try:
                subprocess.run(sshbasecmd + ['rm','-fR',escapedRemoteTmpDir], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as e:
                backupNotifier.add_error("Could not remove remote temp directory:\n" + escapedRemoteTmpDir+"\n\n" +"Backup set skipped.", e)
                continue
            
            # Get latest backup directory
            try:
                p1 = subprocess.Popen(sshbasecmd+['find', escapedRemoteBaseDir, '-mindepth 1', '-maxdepth 1', '-type d'], stdout=subprocess.PIPE)
                p2 = subprocess.Popen(['sort', '-n', '-r'], stdin=p1.stdout, stdout=subprocess.PIPE)
                p3 = subprocess.Popen(['head', '-1'], stdin=p2.stdout, stdout=subprocess.PIPE)
                p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
                p2.stdout.close()  # Allow p2 to receive a SIGPIPE if p3 exits.
                latestBackupDir = p3.communicate()[0].decode("utf-8")
            except subprocess.CalledProcessError as e:
                backupNotifier.add_error("Could not get latest directory. Backup Set Skipped.", e)
                continue

            # Backup to tmp directory, in case backup fails we can easily remove
            if not latestBackupDir:
                # TODO - Using -p (and therefore -a) makes rsync lose its mind. Find out why.
                backupcmd = [rsyncexe, '-rltgoDz', '--stats', '--ignore-errors', '--delete', '--delete-excluded'] + backupSet.excludes + ['-e', sshexe+' -p '+backupTarget.port+' -i '+backupTarget.key, backupSet.localdir, backupTarget.user + "@" + backupTarget.address + ":" + escapedRemoteTmpDir]
            else:
                escapedLatestBackupDir = re.escape(latestBackupDir)
                linkDestTarget = "../" + os.path.basename(os.path.normpath(escapedLatestBackupDir))
                backupcmd = [rsyncexe, '-rltgoDz', '--stats', '--ignore-errors', '--delete', '--delete-excluded'] + backupSet.excludes + ['-e', sshexe+' -p '+backupTarget.port+' -i '+backupTarget.key, '--link-dest='+linkDestTarget, backupSet.localdir, backupTarget.user + "@" + backupTarget.address + ":" + escapedRemoteTmpDir]
                
            try:
                subprocess.run(backupcmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as e:
                backupNotifier.add_error("Rsync failed. Backup set skipped.", e)
                continue
            
            # Move tmp backup dir to final resting place
            escapedRemoteCompletedBackupDir = re.escape(remoteBaseDir + "/" + time.strftime("%Y-%m-%d_%H%M%S"))
            try:
                subprocess.run(sshbasecmd+['mv', escapedRemoteTmpDir, escapedRemoteCompletedBackupDir], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as e:
                backupNotifier.add_error("Renaming temp backup dir from:\n" + escapedRemoteTmpDir+"\n\n" + "to:\n" + escapedRemoteCompletedBackupDir +"\n\n" +"Backup set skipped.", e)
                continue

            # Remove directories that lived past their retention time
            retentionMins = backupSet.retention
            try:
                p1 = subprocess.Popen(sshbasecmd+['find', escapedRemoteBaseDir, '-mindepth 1', '-maxdepth 1', '-type d', '-cmin +' + str(retentionMins)], stdout=subprocess.PIPE)
                p2 = subprocess.Popen(['sort', '-n'], stdin=p1.stdout, stdout=subprocess.PIPE)
                p1.stdout.close()  # Allow p1 to receive a SIGPIPE if p2 exits.
                oldBackupDirs = p2.communicate()[0].decode("utf-8")
            except subprocess.CalledProcessError as e:
                backupNotifier.add_error("Listing directories to get rid of old backups (outside of retention period) failed. Backup set skipped.", e)
                continue
            else:
                if oldBackupDirs:
                    escapedOldBackupDirs = []
                    for oldBackupDir in oldBackupDirs.splitlines():
                        escapedOldBackupDirs.append(re.escape(oldBackupDir))
                    
                    sshcmd = sshbasecmd + ['rm', '-fR'] + escapedOldBackupDirs
                    try:
                        subprocess.run(sshcmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    except subprocess.CalledProcessError as e:
                        backupNotifier.add_error("Removing old backup directories failed. Backup set skipped.", e)
                        continue
            backupNotifier.add_success("Successfully completed backup of:\n" + backupSet.localdir + "\n\nTo:\n"+ remoteBaseDir + "\n\nOn:\n" + backupTarget.address + "\n")

    backupNotifier.send_notification()


if __name__ == "__main__":
    main()

