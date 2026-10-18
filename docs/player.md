# Audio preview

The built-in player is for checking music and lyrics while reviewing the library. It reads the current working file and does not modify audio or metadata.

- Play/pause and stop (return to the beginning).
- Seek bar, elapsed/total time with milliseconds, 10-second backward/forward jumps.
- Exact jump accepts `mm:ss.fff` or `hh:mm:ss.fff`, including decimal commas. Out-of-range values are rejected.
- Volume, mute and 0.5x, 0.75x, 1x, 1.25x, 1.5x, 2x speed. Preferences are saved in this browser. Repeat is per preview and defaults off.
- The selected CUE composition has its own zero-based time scale, a bounded seek range and an automatic stop/repeat at its end. This is a browser preview; extraction provides the separate sample-verified files.
- In the lyrics editor, jumping to a line and stamping the current audio position continue to use full-file timestamps.
- When the player container has keyboard focus: Space toggles playback, Left/Right jumps 10 seconds, Shift+Left/Right jumps 1 second, Home/End seeks to the boundaries, M toggles mute. Buttons, sliders and text fields retain native keyboard behavior. There are no global hotkeys.
- Loading/buffering/error messages include a retry action. Unsupported browser codecs still require an external player or a supported editable derivative. No server transcoding or media download is started by opening this player.

Preview stops when its track/revision is replaced or the editor is closed. Switching between metadata and lyrics tabs preserves playback. It does not provide playlist management or automatic next-track playback.

Verification: frontend unit tests cover CUE bounds, changed/unknown duration, long/precise timestamps, invalid input and stored preferences. Browser acceptance additionally checks actual media playback, seek, speed/mute, stop, line navigation, repeat at a CUE boundary, errors and narrow layouts.
