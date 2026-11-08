WoW Zone Audio Extractor GUI (v0.4.3)
======================================

Files:
- wow_zone_audio_extractor_gui.py
- agent_bridge.py          localhost API on 127.0.0.1:47221
- mcp_server.py            stdio MCP server, registered as wow-ambient

Run:
- Double-click "Start WoW Zone Audio Extractor.bat" in this folder,
  or the desktop shortcut of the same name. A second click focuses the
  window that is already open.
- Or, from PowerShell in this folder: python .\wow_zone_audio_extractor_gui.py

The window listens for an agent at http://127.0.0.1:47221.
MCP tools: wow_status, wow_load_zones, wow_search_zones, wow_open_zone,
wow_recipe, wow_clips, wow_check, wow_extract, wow_preview, wow_stop.

What it does:
- Detects every installed WoW client (Retail, Forever, Classic, and the rest)
  and lets you pick one. The zone list for that build is restored on the next launch.
- Loads zones from current DB2 build
- Lets you browse zones, retrieve their audio maps, and organize clips by:
  - Area
  - Role (Bed, Atmosphere, Detail, Music, Intro)
  - Time of day (Day, Night, Any)
- Reads SoundKit and SoundKitAdvanced so each clip shows the client clock:
  seamless bed, looping atmosphere that re-picks a file, or a one-shot with
  a real gap, fade, pitch shift, and distance
- A day/night pair that shares one sound kit stays one layer labeled Any
- Subzone music is off unless you check it, so a parent zone does not import
  a different playlist from a child area
- Lets you:
  - Filter music vs ambient sounds
  - Search and filter clips by type, time, mood
  - Tag specific clips with ★ indicators
  - Edit clip titles and descriptions
  - Preview individual clips with ▶ Play / ■ Stop controls
  - Check/uncheck clips for extraction (☑ column)
  - Extract checked / tagged / all visible clips
  - Choose output format: mp3 / ogg / flac / wav / m4a
- Friendly name resolution:
  - WoW expansion numbers translated: 120 → BFA, 110 → Legion, 140 → DF, etc.
  - CamelCase splitting: AtalAbasi → Atal Abasi
  - Audio prefixes stripped for cleaner titles
  - FileDataID cross-referenced with community listfile
- Extracts into:
  - <target>\zones\<Family Name>\<Zone Name>\music\<Time>\<Mood>\<Group>\*.{format}
  - <target>\zones\<Family Name>\<Zone Name>\ambient\<Time>\<Mood>\<Group>\*.{format}
- Writes:
  - <target>\wow_zone_audio_export.json
  - <target>\zones\<Family Name>\<Zone Name>\zone_manifest.json
  - manifest.csv files for each group and zone
- When two sound kits share a group name, each folder gets a " (kit N)" suffix
- Weather: with the Weather box ticked, each zone also lists weather presets
  (rain, snow, sandstorm, thunderstorm and more) under the Weather role.
  They start unchecked; check the ones you want to export. WoW's data does
  not say which weather a zone gets, because the server picks it.
- zone_manifest.json gives each group its playback (role, time of day,
  weather) and import_defaults with pitch range, pan spread, first delay and
  music silence. KrakenScape uses these to build Day, Night and weather moods.
- Extracting again adds to the manifests and export index. Clips exported
  before are kept, and a clip exported again replaces its older file.

Notes:
- Needs internet access (reads DB2 metadata + pulls audio by FileDataID)
- First friendly-name lookup may download the community WoW listfile cache
- Needs ffmpeg (auto-detected from PATH or common install paths)
- The zone list and sound indexes are saved per game build and restored
  on the next launch. Load Zones again only when the build changes.
- First run is slower because tables are downloaded and cached
- Time-of-day / mood / type labels are inferred from DB2 metadata and can be edited
- KrakenScape can import a zone bundle via the exported folder
