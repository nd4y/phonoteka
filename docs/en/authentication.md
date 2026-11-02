## Access without sign-in

`AUTH_ENABLED=true` is the default. An administrator can explicitly set `AUTH_ENABLED=false` in Docker configuration and recreate the app when access is controlled by the network or reverse proxy. Anyone who can reach the address receives full application access, including file writes and settings. This is not a guest mode. The UI shows that sign-in is disabled and hides logout controls; there is no browser toggle.

Mutations still require an exact `Origin` matching `APP_URL` and the `X-CSRF-Token` returned by `/api/auth/session`. Open mode creates no anonymous cookies or persistent sessions. Requests with Bearer credentials still enforce token validity and scopes; metrics and inference retain their separate keys. Provider permissions, file-write restrictions, publication checks and existing jobs remain in effect.

Restore `AUTH_ENABLED=true` and recreate the app to require sign-in again. Existing credentials remain intact; the first protected startup still requires `OWNER_PASSWORD_FILE`. Values other than `true`, `false`, `1`, `0` fail startup validation. Refresh open tabs after a mode change or restart to obtain current access state and CSRF token.
