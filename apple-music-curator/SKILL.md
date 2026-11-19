---
name: apple-music-curator
description: Recommend songs from a Mac Music library, curate Apple Music playlists, resume interrupted additions without duplicates, and loop a playlist on request.
---

# Apple Music Curator

1. Always reply to the user in Chinese. Preserve original song titles, artist names, and playlist names.
2. For a recommendation or shortlist request, return numbered picks. For a playlist request, use the selected songs or curate the requested set, then build and verify the playlist. “Make these a playlist” selects the full preceding list. Numbered selections apply only to those entries. Treat likes as feedback. The reference playlist and destination may differ.

## Find and verify songs

Run commands from this skill directory. Keep library snapshots in a private temporary directory. When macOS requests Automation access, explain which Music operation needs it.

- Use `osascript -l JavaScript scripts/library.js playlists`, then `playlist ID` or `index` with the same script. Resolve duplicate names by persistent ID and contents. If no reference is specified, choose a representative playlist and identify it.
- Use favorites, explicit ratings, repeated plays and the listening context. Check `unavailableFields`. An automatic rating, one skip or zero plays is weak evidence. Prioritize explicit preferences and retain feedback across sessions only when requested.
- Pick songs with an audible connection to a reference track. For discovery, include unfamiliar artists. Identify the artist, album and recording, including language, live performances and re-recordings.
- Use Music search or `python3 scripts/catalog.py search --country HK --term 'artist title'`. Use `lookup --country HK --id ID` for catalog metadata and `page --country CN --id ID` to verify the song in the intended storefront. Substitute the actual region. If a regional search is empty, use another region for leads and verify locally. State any assumed region. Account availability is determined in the signed-in client.
- Run `osascript -l JavaScript scripts/shortlist.js library.json candidates.json reference.json`. The optional reference also covers songs outside the main library. Candidates use the catalog output's tracks array and may include title_aliases and artist_aliases. For new songs prefer no_title_match, skip same_title_artist and inspect review_version_or_artist. Give each pick a stable number, version, verified song link and a specific reason.

## Build or resume

Save the ordered selection before the first playlist change. Use the catalog page JSON format. Keep name, artist and album for every song. For catalog tracks keep storefront, catalog_id, album_id and verified album_track_url. For library-only tracks keep library_id. Resolve missing album links with the regional page command.

Use the requested destination. For a new unnamed playlist, use the user's local date, taste or context, and style. Check namesakes by ID and contents. For a distinct new playlist use a distinguishing suffix. Ask only if the intended existing target is ambiguous.

Run these commands with `python3 scripts/session.py`:

- New playlist: `start --tracks picks.json --new-playlist NAME`, then `target --run RUN` using the returned run number. Existing target: `start --tracks picks.json --playlist-id ID`.
- “Continue the last playlist”: run `resume` immediately. This reads the saved selection and the original playlist by persistent ID, including after a rename. Stop if that ID is inaccessible. State defaults to ~/.local/state/apple-music-curator under XDG_STATE_HOME, with APPLE_MUSIC_CURATOR_STATE_DIR as an override.
- **ready**: handle only next. Open its verified album_track_url with `open -a Music "$url"` or the page's Open in Music control. Verify the artist, album, version and track row, then locate More → Add to Playlist and the exact destination. For a library track use `python3 scripts/playlist.py preview --playlist-id ID --track-id LIBRARY_ID`.
- Immediately before clicking Add or running the same playlist command with apply, run `claim --run RUN --track TRACK_ID` (catalog_id, or library_id for library-only tracks). Use each fresh nonempty permit for one add attempt. Run `resume --run RUN` afterward. Skip confirmed tracks and preserve other entries and their order.
- **awaiting_readback**: wait for sync and read again. Inspect blocking_run if returned. Keep an uncertain send pending. Use `release --run RUN --permit PERMIT --not-sent` only with positive evidence that the add was never sent. For **review**, resolve the recording ambiguity first.
- **needs_target**: run target once. For **target_uncertain**, establish which playlist the original create produced before using `bind --run RUN --playlist-id ID`. A matching name alone is insufficient.
- **complete**: report the verified playlist name and track count. Provide its actual Copy Link URL when available, otherwise its location in Music → Library → Playlists. Keep sharing settings intact. Local IDs identify objects inside Music.
- For removal requests, identify the destination and exact recordings. Use Remove from Playlist and verify the result in the same playlist.

## Playback

Only start playback when requested. After verifying the destination and its contents, run `python3 scripts/playback.py loop --playlist-id ID`. Use `python3 scripts/playback.py status` to confirm the target ID, playing state and repeat-all mode. Report playlist creation and playback separately if either step fails.

## Learn listening preferences

When the user requests ongoing personalization, use `python3 scripts/taste.py`. It stores preferences, numbered selections and feedback in the private local state database used by sessions. Keep personal examples, listening data and database files outside the skill directory.

- Start with `show --scope work` or the relevant listening context. Use `note --scope work --text TEXT` for a contextual preference and `note --text TEXT` for a general preference. Label inferences from the library as such. Use the current request to guide the selection.
- Add comparable `tags` to candidate tracks, using short labels for language, energy, vocals, instrumentation, groove or genre (such as `energy:low`, `texture:piano` or `vocal:soft`). Reuse established tags and omit uncertain ones. Run `rank --scope work --tracks candidates.json`. Consider the score and its matching traits alongside freshness, language mix and the specific request. Leave room for unfamiliar artists and untested styles.
- Use `remember --scope work --batch LABEL --tracks picks.json` to preserve the exact numbers shown to the user. For track feedback use `feedback --batch LABEL --number N --vote like|dislike|clear` with an optional `--note`. Use the batch scope by default and `--scope all` for an explicitly general preference. Save feedback about a style or the overall sequencing as a scoped note. An unmentioned song has no rating.
- For a built playlist use `observe --batch LABEL --run RUN` after verification to establish play-count baselines. On later invocations, refresh relevant playlists from the `sources` returned by `show` before ranking. Replays visible in Mac Music become a weak signal after two additional plays. Prefer explicit feedback and never infer dislike from no plays, skips or unavailable data.
