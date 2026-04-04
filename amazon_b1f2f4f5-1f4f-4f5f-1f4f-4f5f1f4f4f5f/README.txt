=== GOG Galaxy Amazon Luna Plugin ===

INSTALLATION:
1. Extract this folder to:
   C:\Users\[YourUsername]\AppData\Local\GOG.com\Galaxy\plugins\installed\

2. Folder structure should be:
   C:\Users\...\plugins\installed\amazon-luna\
   ├── plugin.py
   ├── manifest.json
   └── requirements.txt

3. Close GOG Galaxy completely
4. Reopen GOG Galaxy
5. Look for "Amazon Luna" in your integrations
6. Click to set up authentication

If plugin doesn't appear:
- Check that manifest.json has "guid" field with unique value
- Verify folder name is exactly "amazon-luna" (lowercase, hyphenated)
- Check GOG Galaxy logs at:
  C:\Users\[YourUsername]\AppData\Local\GOG.com\Galaxy\logs\

For troubleshooting, see:
https://github.com/gogcom/galaxy-integrations-python-api/blob/master/README.md
