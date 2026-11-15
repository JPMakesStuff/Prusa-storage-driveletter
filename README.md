# Prusa Storage Driveletter

Create a mapped drive to multiple Prusa printers USB storage

This is an entirely local/offline (as in no cloud, no Internet) solution.

 - Modify the printers.example.json with the details of your printers, name this file printers.json
 - Simply put <b>printers.json</b> and <b>PrusaDrive.exe</b> in the same folder and run it, the drive letter is created automatically with sub-folders being each one of your printers
 - Copy files to/from the USB stick without wearing out the USB port anymore!
 - Pick Exit (Unmount Drive) from the system tray icon to close
 - Optionally put a shortcut to PrusaDrive.exe in your startup folder if you want it to run every time you login

Prerequisites:

 - [PrusaLink](https://help.prusa3d.com/article/prusa-connect-and-prusalink-explained_302608) must be enabled on your printer(s), it's built-in to modern firmwares, from the LCD go to Settings > Network > PrusaLink > Enable it and make note of the password and IP address of your printer, this requires HTTP port 80 be accessible on the LAN between the printers and your Windows machine
 - [winfsp-2.1.25156.msi](https://github.com/winfsp/winfsp/releases/download/v2.1/winfsp-2.1.25156.msi) must be installed on the Windows machine where you want to run PrusaDrive.exe
 - [python-3.11.9-amd64.exe](https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe) should be installed only if you want to build the .exe yourself from the .py script (use the build_prusadrive.bat)

Current limitations:

 - Folders on the printers USB stick that have +100 files, you may need to wait awhile or it may timeout and not list the files or it may say something about not enough permissions, but that is not accurate
 - Windows file copy percentage appears to hang on large files - this is not accurate, just wait longer for it to finish
 - Cannot create new folders or delete folders on the USB stick - do that by inserting the USB stick in your computer instead
 - Cannot overwrite or delete a file that is currently displayed on the printers LCD display - no fix for this, go back to the home screen (or stop printing that file), then it will work

Tested with Prusa XL firmware 6.4.1, MK4S firmware 6.5.3, COREONE firmware 6.5.3 - if API endpoints change in later versions this software will have to be updated

[END]
