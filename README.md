# Prusa Storage Driveletter

Create a mapped drive to multiple Prusa printers USB storage

 - Simply put PrusaDrive.exe and printers.json in a folder
 - Modify the .json with the details of your printers on the LAN
 - Enjoy not wearing out your USB stick anymore!

Entirely local/offline (as in no cloud, no Internet) solution, does require PrusaLink be enabled on your printers and HTTP port 80 accessible on the LAN between the printers and your Windows machine.

Current limitations:

 - Folders on the printers USB stick that have +100 files, it may timeout or just not list the files or say something about not enough permissions - this is not accurate
 - Windows file copy percentage appears to hang on large files - this is not accurate, just wait longer for it to finish
 - Cannot create new folders on the USB stick - do that by inserting the USB stick in your computer instead
 - Cannot overwrite or delete a file that is currently displayed on the printers LCD display - no fix for this, go back to the home screen (or stop printing that file), then it will work

[END]
