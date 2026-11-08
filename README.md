# WoW Zone Audio Extractor

Windows desktop app for browsing World of Warcraft zone ambience and music, then extracting the clips you choose.

The app reads zone and sound-kit data from [wago.tools](https://wago.tools) DB2 tables. It lists every installed WoW client and remembers the zone list and sound index for each build. Preview and extract use ffmpeg.

No game audio is in this repository. Extraction writes clips on your machine under `zones\`.

## Run

Double-click `Start WoW Zone Audio Extractor.bat`, or from this folder:

```powershell
python .\wow_zone_audio_extractor_gui.py
```

Python 3.14 and ffmpeg are required. The window also listens on `127.0.0.1:47221` for the local agent. `mcp_server.py` is the stdio MCP server.

See `wow_zone_audio_extractor_gui_README.txt` for the layout, roles, and export format.

World of Warcraft and its audio are owned by Blizzard Entertainment.
