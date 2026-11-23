# Native Windows services (WinSW)

The selected deployment uses WinSW 2.12.0 and Caddy 2.11.9, running native processes with direct
CUDA and filesystem access. It replaces the earlier Task Scheduler setup.

**Deployment status (2026-11-23):** both services are installed and running. Models loaded on the
RTX 4080. Verified HTTPS certificate trust, unauthenticated rejection (401), hybrid search,
and reading the matching cited page. All 71 automated tests passed, including request telemetry
and 90-day retention. Live startup and post-request retention probes also passed. Linux client installation
and an actual Windows reboot have not yet been verified.

| Setting | Value |
|---|---|
| HTTPS URL | `https://hal5000.fios-router.home` |
| Windows interface/address | `Ethernet`, `192.168.1.243` |
| Allowed client network | **`192.168.1.0/24`** |
| Initial client | Zarathustra (currently `192.168.1.228`) |
| API listener | `127.0.0.1:8765` only |
| Services | `LibrarianApi`, `LibrarianHttps` |
| Service binaries/config | `C:\ProgramData\Librarian\Services` |
| Private state | `C:\ProgramData\Librarian\State` |

**Current mode: LAN trust. Any device on `192.168.1.0/24` may use the read-only API/MCP
without a token. HTTPS and the subnet restrictions remain enabled.**
Both Windows Firewall and Caddy enforce the client network. Firewall scope includes the specific
interface, local address and TCP port 443, across network profiles. This works even though this
Ethernet connection is currently classified Public; no system-wide profile change is required.
There is no public tunnel, router forwarding, or IPv6 listener. Reserve the Windows server's
address in DHCP; clients may change addresses within the subnet.

## Install

From `F:\repos\librarian`, download and verify the pinned official binaries:

```powershell
scripts\api-download.ps1
```

Then run in an **elevated Windows PowerShell** window:

```powershell
Set-Location F:\repos\librarian
scripts\api-startup.ps1 -WinSWExe .runtime\windows\WinSW-x64.exe -CaddyExe .runtime\windows\caddy.exe
```

Defaults are the values above. The installer creates private directories, generates WinSW XML,
installs the services, grants filesystem permissions, adds the
firewall rule, and starts both services. It refuses to overwrite existing installation/state
directories or services. For a failed partial install, inspect the error and existing state before
retrying; do not delete credentials or CA keys to make the installer run again. After inspection,
`-Resume` can continue a partial install with matching stopped service identities and paths;
it preserves existing credentials and certificates. It is not an upgrade command for running services.

Services run as separate virtual accounts, `NT SERVICE\LibrarianApi` and
`NT SERVICE\LibrarianHttps`, with no user password or interactive logon required. The API gets
read access to the repository, base Python installation, `D:\Models\huggingface`, and the configured
library data directory. Its library `logs` and `cache` directories are writable. Caddy gets its
own writable certificate directory. Service binaries/configurations are not service-writable.
The original PDFs are not served. Existing inherited filesystem permissions are not removed.

The API starts Python directly; HTTPS starts Caddy directly. Both have delayed automatic start,
restart on failure, and rotating logs. HTTPS depends on the API service, but **Running does not
mean models are ready**; poll `/v1/status` until `ready` is true.

The XML generation can be reviewed separately without installing anything:

```powershell
scripts\api-service-config.ps1 -OutputDirectory .runtime\preview -StateDirectory C:\ProgramData\Librarian\State -CaddyExe C:\ProgramData\Librarian\Services\caddy.exe
```

## Connect Zarathustra

After installing the public CA below, no token setup is needed. Test with:

```bash
curl https://hal5000.fios-router.home/v1/status
```

Use this in `~/.codex/config.toml` and restart Codex:

```toml
[mcp_servers.librarian]
url = "https://hal5000.fios-router.home/mcp"
tool_timeout_sec = 75
```

Remove any old `bearer_token_env_var` setting. `codex mcp login` is not needed.

To apply this mode to an existing Windows installation, run elevated:

```powershell
scripts\api-update-services.ps1 -AuthMode Lan
```

Updates without `-AuthMode` preserve the installed mode. To restore token authentication,
use `-AuthMode Bearer` with a provisioned token store; token-management details follow.

### Public CA certificate

The deployed public CA certificate is tracked at
[`config/certs/librarian-root.crt`](../config/certs/librarian-root.crt). After pulling the repository
on Zarathustra, install it from the repository root (Debian/Ubuntu):

```bash
sha256sum config/certs/librarian-root.crt
sudo install -m 644 config/certs/librarian-root.crt /usr/local/share/ca-certificates/librarian-root.crt
sudo update-ca-certificates
```

Expected file SHA-256: `6db10bae8eb72a08d72b202c0ae73e194c37a1c966cf2fa1ca89ff1946d931ca`.
Only the public certificate is in Git. The CA private key stays on HAL5000.

### Optional bearer-token mode: where to set the token

**Skip the remaining token instructions while LAN-trust mode is enabled.**

**On Zarathustra, store the actual token in `~/.config/librarian/client.token`.** The existing
token value is in `F:\repos\librarian\.runtime\zarathustra\client.token` on Windows (or the
administrator-only `C:\ProgramData\Librarian\State\zarathustra.token`). Transfer that file securely,
or paste its contents at the hidden prompt below. Run as the Linux user who runs Codex:

```bash
mkdir -p ~/.config/librarian
chmod 700 ~/.config/librarian
read -rsp 'Paste Librarian token: ' LIBRARIAN_TOKEN
printf '\n'
(umask 077; printf '%s\n' "$LIBRARIAN_TOKEN" > ~/.config/librarian/client.token)
chmod 600 ~/.config/librarian/client.token
export LIBRARIAN_TOKEN
```

For each new shell, load the saved value before starting Codex:

```bash
export LIBRARIAN_TOKEN="$(cat ~/.config/librarian/client.token)"
codex
```

To load it automatically in Bash, add **that export line** to `~/.bashrc`; do not put the literal
token in shell startup files or Git. A running Codex process must be restarted from the shell
with the variable set. `bearer_token_env_var = "LIBRARIAN_TOKEN"` in `~/.codex/config.toml`
names the environment variable; it is not the place to paste the token itself. The full MCP
configuration is below. If using a launcher other than the shell, its process must receive the
same environment variable.

### Connection files and verification

Using an elevated shell on Windows, securely transfer these files to the Linux user:

- `C:\ProgramData\Librarian\State\zarathustra.token` → `~/.config/librarian/client.token`
- `C:\ProgramData\Librarian\State\caddy\pki\authorities\local\root.crt` → `librarian-root.crt`

For this installation, user-private copies are already prepared in
`F:\repos\librarian\.runtime\zarathustra` with Linux instructions and a Codex configuration fragment.
This ignored folder is accessible only to the Windows owner and SYSTEM. Transfer its contents
through a secure channel; do not commit or publish the token. The public root certificate's
SHA-256 fingerprint is `6DB10BAE8EB72A08D72B202C0AE73E194C37A1C966CF2FA1CA89FF1946D931CA`.

The first is secret; the second is the public CA certificate. Never transfer the CA private key.
After successful transfer, remove the server's client token copy; the server uses only hashes in
`State\auth\tokens.json`. On Debian/Ubuntu Linux:

```bash
chmod 600 ~/.config/librarian/client.token
sudo install -m 644 librarian-root.crt /usr/local/share/ca-certificates/librarian-root.crt
sudo update-ca-certificates
export LIBRARIAN_TOKEN="$(cat ~/.config/librarian/client.token)"
export LIBRARIAN_URL='https://hal5000.fios-router.home'
printf 'Authorization: Bearer %s\n' "$LIBRARIAN_TOKEN" |
  curl --fail-with-body --header @- "$LIBRARIAN_URL/v1/status"
```

The router hostname was verified to resolve to `192.168.1.243` on Windows. Verify resolution on
Zarathustra too; use a hosts entry if its resolver does not use the router's DNS. Do not disable
certificate verification. For other Linux distributions, use the equivalent CA trust procedure.

Add to `~/.codex/config.toml`, and start Codex from the shell with the token exported:

```toml
[mcp_servers.librarian]
url = "https://hal5000.fios-router.home/mcp"
bearer_token_env_var = "LIBRARIAN_TOKEN"
tool_timeout_sec = 75
```

See [REST examples](network.md#linux-rest-and-codex) for searches and citation verification.
API token authentication is independent of network access; provision separate tokens for other
clients. In an elevated shell, revoke Zarathustra immediately:

```powershell
scripts\run.ps1 api token-revoke --tokens C:\ProgramData\Librarian\State\auth\tokens.json --client zarathustra
```

To rotate, `token-add` with a new client name/output file, transfer it, switch the client, then
revoke the old name. Use an output file under the private State directory. The auth directory's
inherited read grant ensures the API can read atomically replaced token stores.

## Operate

Run service control commands elevated:

```powershell
Get-Service LibrarianApi,LibrarianHttps
Restart-Service LibrarianApi -Force
Start-Service LibrarianHttps
Stop-Service LibrarianHttps
Stop-Service LibrarianApi
```

Service logs and telemetry are under `F:\repos\librarian\.runtime\logs` (gitignored).
Daily UTC `events-YYYY-MM-DD.jsonl` files contain runtime events, HTTP request records and
REST/MCP operation results. Records include client name/IP, request ID, route, status, duration,
response bytes, and search hit counts, but omit tokens, query text and book text. WinSW output
rotates daily under `winsw\LibrarianApi` and `winsw\LibrarianHttps`.

The API deletes log files older than 90 days by last-modified time at startup and after every
HTTP request, including authentication failures. Cleanup is restricted to log files under this
root, skips symbolic links/junctions, and leaves credentials/certificates untouched. Files still
open on Windows are retried on subsequent requests. Earlier service logs are migrated to
`legacy` and use the same retention policy. The independent extraction/ingestion CLI logs
remain in the library data directory.

To update an existing installation and migrate its logs, run elevated:

```powershell
scripts\api-update-services.ps1
```

This briefly stops/restarts the services without changing tokens, certificates or network scope. Restart after indexing to refresh cached vectors and pages. Keep Windows awake and
verify search after a reboot. If old `Librarian-API`/`Librarian-HTTPS` scheduled tasks were ever
installed, stop and remove those specific tasks before switching to these services.

WinSW configuration reference: [WinSW 2.x XML](https://github.com/winsw/winsw/blob/v2/doc/xmlConfigFile.md).
