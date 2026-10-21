Muse2API Cookie Importer Extension — Installation Guide
=====================================

This extension does exactly one thing: it syncs the muse.ai login cookies
from your browser to your muse2api service. No Python install, no terminal needed.


1. Unzip
--------
Unzip muse2api-extension.zip into a directory you won't delete, for example:

    Windows : D:\muse2api-extension
    macOS   : ~/Documents/muse2api-extension

After unzipping you should see three files: manifest.json, popup.html and popup.js.


2. Load it in Chrome / Edge
---------------------------
Chrome:
  1. Type  chrome://extensions  in the address bar and press Enter
  2. Turn on the "Developer mode" switch in the top-right corner
  3. Click "Load unpacked" in the top-left corner
  4. Select the folder you unzipped in step 1 (the folder itself, not a single file inside it)
  5. A puzzle-piece icon appears in the toolbar; pin the extension to the toolbar (optional but handy)

Edge:
  1. Type  edge://extensions  in the address bar and press Enter
  2. Turn on "Developer mode" in the bottom-left corner
  3. Click "Load unpacked"
  4. The rest is the same as above

Other Chromium-based browsers (Brave / Vivaldi / 360 Speed, etc.) follow similar steps.


3. Usage
--------
1. First open https://muse.ai/ in this browser and log in,
   until you can see the chat screen.
2. Go to the top of the "Account Pool" page in the muse2api admin console and copy the BASE URL and API Key.
3. Click the extension icon in the browser toolbar and fill in these two values (only needed once; they are remembered).
4. Click "Read and import". When you see "✓ Imported successfully", you're done.


4. FAQ
------------
Q: It says "No muse.ai cookies found"
A: This browser is not logged in to muse.ai yet. Open https://muse.ai/ and log in first.

Q: It says "Missing core items such as hatch_sess"
A: Same as above — the login is not complete. Make sure you can see the chat screen, then click again.

Q: It says "Invalid API Key (the service returned 401)"
A: Copy the API Key from the admin console again. Note that the key starts with m2a_.

Q: It reports a CORS / network error
A: Check whether the service URL is https, and whether the service itself can be opened
   (try visiting <service URL>/admin directly in the browser).

Q: Will my cookies be sent anywhere else?
A: No. The extension only has the cookies and storage permissions, and all its code is in popup.js.
   You can check it yourself: it sends a single POST to the service URL you entered, and makes no other requests.


5. Uninstall
--------
chrome://extensions → find "Muse2API Cookie Importer" → Remove.
