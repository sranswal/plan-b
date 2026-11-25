"""Desktop OAuth and OS credential storage for the optional Google provider."""

import argparse
import json
import logging
from pathlib import Path
from threading import Lock
from urllib.parse import quote

from services.config import PROJECT_ROOT, setting


SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"
API = "https://www.googleapis.com/calendar/v3"
VAULT_SERVICE = "Plan B Google Calendar"
VAULT_ACCOUNT = "desktop"
RECONNECT = "Google Calendar authorization is unavailable. Run the Plan B Google Calendar connect command again."
SECURE_BACKENDS = {
    "keyring.backends.Windows", "keyring.backends.macOS", "keyring.backends.SecretService", "keyring.backends.kwallet",
}


class GoogleError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Vault:
    def __init__(self, service=VAULT_SERVICE):
        self.service = service
        try:
            import keyring

            self.backend = keyring.get_keyring()
        except Exception:
            raise GoogleError(503, "Install the optional Google dependencies and unlock your OS credential store.") from None
        if type(self.backend).__module__ not in SECURE_BACKENDS:
            raise GoogleError(503, "A supported OS credential store is required for Google Calendar.")

    def load(self):
        try:
            value = self.backend.get_password(self.service, VAULT_ACCOUNT)
            return json.loads(value) if value else None
        except Exception:
            raise GoogleError(503, "Could not read the Plan B entry in your OS credential store.") from None

    def save(self, profile):
        try:
            self.backend.set_password(self.service, VAULT_ACCOUNT, json.dumps(profile))
        except Exception:
            raise GoogleError(503, "Could not save the Plan B entry in your OS credential store.") from None

    def clear(self):
        try:
            self.backend.delete_password(self.service, VAULT_ACCOUNT)
        except Exception:
            raise GoogleError(503, "Could not remove the Plan B entry in your OS credential store.") from None


def google_request(credentials, method, path, *, api=API, reconnect=RECONNECT, service="Google Calendar", **kwargs):
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import AuthorizedSession
    from requests import RequestException

    try:
        # Expired tokens refresh before the request. Do not replay a rejected POST.
        with AuthorizedSession(credentials, refresh_status_codes=(), refresh_timeout=15) as session:
            response = session.request(method, api + path, timeout=15, allow_redirects=False, **kwargs)
    except RefreshError as error:
        if error.retryable:
            raise GoogleError(503, f"{service} could not refresh its authorization. Try again later.") from None
        raise GoogleError(401, reconnect) from None
    except (RequestException, TransportError):
        raise GoogleError(503, f"Could not reach {service}. The operation could not be confirmed.") from None
    status = response.status_code
    if status == 401:
        raise GoogleError(401, reconnect)
    if status == 403:
        if service == "Google Tasks":
            try:
                error = response.json()["error"]
                reasons = {item.get("reason") for key in ("details", "errors")
                           for item in error.get(key, []) if isinstance(item, dict)}
            except (ValueError, KeyError, TypeError, AttributeError):
                reasons = set()
            if reasons & {"SERVICE_DISABLED", "accessNotConfigured"}:
                raise GoogleError(403, "Google Tasks API is disabled. Enable tasks.googleapis.com in the Cloud project that owns the Desktop OAuth client, then reconnect.")
            if reasons & {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"}:
                raise GoogleError(403, "Google Tasks permission is missing. Add the https://www.googleapis.com/auth/tasks scope in Google Auth Platform, then reconnect and grant access.")
        raise GoogleError(403, f"{service} denied access. Check permissions and API configuration.")
    if status in {404, 410}:
        raise GoogleError(404, f"{service} could not find the requested resource.")
    if status == 409:
        raise GoogleError(409, f"{service} reported a conflict.")
    if status == 429 or status >= 500:
        raise GoogleError(503, f"{service} is temporarily unavailable. The operation could not be confirmed.")
    if not 200 <= status < 300:
        raise GoogleError(502, f"{service} rejected the request.")
    try:
        result = response.json()
        if isinstance(result, dict):
            return result
    except ValueError:
        pass
    raise GoogleError(502, f"{service} returned an invalid response.")


class GoogleConnection:
    scope = SCOPE
    api = API
    reconnect = RECONNECT
    service = "Google Calendar"

    def __init__(self):
        self.lock = Lock()

    def vault(self):
        return Vault()

    def profile(self):
        profile = self.vault().load()
        if profile is None:
            raise GoogleError(401, self.reconnect)
        return profile

    def request(self, profile, method, path, **kwargs):
        from google.oauth2.credentials import Credentials

        with self.lock:
            current = self.profile()
            if current["connection_id"] != profile["connection_id"]:
                raise GoogleError(409, "Google Calendar selection changed. Preview the action again.")
            stored = current["credentials"]
            try:
                credentials = Credentials.from_authorized_user_info(stored, scopes=[self.scope])
            except ValueError:
                raise GoogleError(401, self.reconnect) from None
            try:
                return google_request(credentials, method, path, api=self.api, reconnect=self.reconnect, service=self.service, **kwargs)
            finally:
                updated = json.loads(credentials.to_json())
                if updated.get("refresh_token") and any(updated.get(key) != stored.get(key) for key in ("token", "refresh_token", "expiry")):
                    vault = self.vault()
                    latest = vault.load()
                    if latest and latest["connection_id"] == current["connection_id"]:
                        vault.save({**current, "credentials": updated})


def desktop_credentials(client, scope):
    from google_auth_oauthlib.flow import InstalledAppFlow

    try:
        flow = InstalledAppFlow.from_client_secrets_file(client, scopes=[scope], autogenerate_code_verifier=True)
        if (flow.client_type != "installed" or flow.client_config.get("auth_uri") not in {
                "https://accounts.google.com/o/oauth2/auth", "https://accounts.google.com/o/oauth2/v2/auth"}
                or flow.client_config.get("token_uri") != "https://oauth2.googleapis.com/token"):
            raise ValueError
        credentials = flow.run_local_server(
            host="127.0.0.1", port=0, timeout_seconds=180, open_browser=True,
            authorization_prompt_message=None, success_message="Return to the Plan B terminal.", prompt="consent",
        )
    except Exception:
        raise GoogleError(401, "Google sign-in did not complete. Check the Desktop app client file and consent, then try again.") from None
    if not credentials.refresh_token or not credentials.has_scopes([scope]):
        raise GoogleError(401, "Google did not grant the requested offline access. Connect again.")
    return credentials


def connect(args, vault):
    import uuid

    if vault.load() is not None:
        raise GoogleError(409, "Disconnect the current calendar connection before selecting another calendar.")
    if not args.calendar_id.strip() or args.calendar_id == "primary":
        raise GoogleError(422, "Provide the explicit calendar ID from Google Calendar settings.")
    credentials = desktop_credentials(args.client, SCOPE)
    calendars = list(dict.fromkeys([args.calendar_id, *args.busy_calendar]))
    for calendar in calendars:
        result = google_request(credentials, "GET", f"/calendars/{quote(calendar, safe='')}/events", params={"fields": "accessRole", "maxResults": 1})
        if result.get("accessRole") != "owner":
            raise GoogleError(403, "Choose calendars owned by the consenting Google account.")
    vault.save({"connection_id": uuid.uuid4().hex, "calendar": args.calendar_id, "calendars": calendars, "credentials": json.loads(credentials.to_json())})
    print(f"Connected. One selected write calendar and {len(calendars)} calendar(s) included in availability.")


def disconnect(vault, profile):
    from requests import RequestException, post

    revoked = False
    try:
        response = post("https://oauth2.googleapis.com/revoke", data={"token": profile["credentials"]["refresh_token"]}, timeout=15, allow_redirects=False)
        revoked = response.status_code == 200
    except RequestException:
        pass
    vault.clear()
    print("Local Google Calendar connection removed.")
    if not revoked:
        print("Google did not confirm revocation. Remove Plan B access in your Google Account connections.")


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("connect")
    setup.add_argument("--client", help="Override PLAN_B_GOOGLE_CLIENT_FILE")
    setup.add_argument("--calendar-id", help="Override PLAN_B_GOOGLE_CALENDAR_ID")
    setup.add_argument("--busy-calendar", action="append", default=[])
    commands.add_parser("status")
    commands.add_parser("disconnect")
    args = parser.parse_args()
    if args.command == "connect":
        if args.client is None:
            client = setting("PLAN_B_GOOGLE_CLIENT_FILE", "").strip()
            if not client:
                setup.error("Set PLAN_B_GOOGLE_CLIENT_FILE in .env or pass --client.")
            args.client = str(PROJECT_ROOT / Path(client).expanduser())
        if args.calendar_id is None:
            args.calendar_id = setting("PLAN_B_GOOGLE_CALENDAR_ID", "").strip()
            if not args.calendar_id:
                setup.error("Set PLAN_B_GOOGLE_CALENDAR_ID in .env or pass --calendar-id.")
    try:
        vault = Vault()
        if args.command == "connect":
            connect(args, vault)
        else:
            profile = vault.load()
            if args.command == "disconnect" and profile:
                disconnect(vault, profile)
            else:
                print("Stored connection present." if profile else "Google Calendar is disconnected.")
    except (GoogleError, ImportError) as error:
        parser.exit(1, f"{error}\n" if isinstance(error, GoogleError) else "Install the optional Google dependencies first.\n")


if __name__ == "__main__":
    main()
