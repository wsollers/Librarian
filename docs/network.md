# LAN deployment

**For the selected WinSW deployment, start with [Windows services](windows-services.md).**
The foreground instructions below are for manual diagnosis or an alternate installation.

The backend binds only to `127.0.0.1:8765`. Caddy terminates HTTPS on a specific Windows LAN
IPv4 address. Windows Firewall permits only approved Linux client addresses. No VPN, cloud
relay, public tunnel, or router port forwarding is required.

## Prepare the account and credentials

Run `scripts\bootstrap.ps1` to install the API dependencies. Verify local search with
`scripts\run.ps1 search "Lipschitz"` before configuring remote access.

Use a dedicated non-administrator Windows account for unattended operation. Give it read/execute
access to the repo, `.venv`, model cache, SQLite database (including any existing journal files),
and extracted pages. Grant write access to the library's `logs` and `cache` directories and its
own private API/Caddy state directory. Configure its model-cache location and library paths:
another user's Hugging Face cache may not be available to it. Avoid simultaneous desktop MCP
instances if duplicated models would exhaust GPU memory.

Run these commands **as the account that will run the service**. The directory creation script
limits access to that account and SYSTEM; it refuses to overwrite an existing directory:

```powershell
$apiState = "$env:LOCALAPPDATA\LibrarianApi"
scripts\api-secrets.ps1 -Directory $apiState
scripts\run.ps1 api token-add --tokens "$apiState\tokens.json" --client linux-codex --output "$apiState\linux-codex.token"
```

Transfer the client `.token` file securely to `~/.config/librarian/client.token` on Linux and
restrict it with `chmod 600`. Remove the server copy of that client token after transfer.
Keep `tokens.json`, which contains only hashes. Do not commit secrets or place token values
in command arguments or logs. Provision a separate client name and output file for each client.

Revoke immediately without reloading models:

```powershell
scripts\run.ps1 api token-revoke --tokens "$apiState\tokens.json" --client linux-codex
```

For rotation, provision a new client name, switch the client, then revoke the old name. Run
provisioning commands one at a time. An empty store denies requests and prevents new server
startup; missing/corrupt stores fail closed.

## Start HTTPS on the LAN

Reserve DHCP addresses for Windows and Linux. Configure a LAN DNS record or Linux hosts entry
for the Windows address. All addresses and account names below are **examples to replace**.

Start the backend in a PowerShell window under the intended account:

```powershell
scripts\api-host.ps1 -Mode api -StateDirectory $apiState -Hostname hal5000.fios-router.home
```

Install the official Windows [Caddy binary](https://caddyserver.com/download) at a stable path.
In another window under the same account:

```powershell
$apiState = "$env:LOCALAPPDATA\LibrarianApi"
scripts\api-host.ps1 -Mode proxy -StateDirectory $apiState -Hostname hal5000.fios-router.home -LanAddress 192.168.1.243 -CaddyExe C:\Tools\Caddy\caddy.exe
```

The included [Caddyfile](../config/Caddyfile.example) binds only the supplied IPv4 address,
disables the admin listener and HTTP redirects, and supports MCP streaming. Caddy's local CA
and private keys live under `$apiState\caddy`. Transfer only its public root certificate,
`$apiState\caddy\pki\authorities\local\root.crt`, to clients through a trusted channel.
See [internal TLS](https://caddyserver.com/docs/caddyfile/directives/tls) and
[interface binding](https://caddyserver.com/docs/caddyfile/directives/bind).

On Debian/Ubuntu, after transferring and verifying that public certificate:

```bash
sudo install -m 644 librarian-root.crt /usr/local/share/ca-certificates/librarian-root.crt
sudo update-ca-certificates
```

Use your distribution's equivalent elsewhere. Both curl and Codex must trust the CA. If needed,
set `SSL_CERT_FILE` to the updated PEM CA bundle in the shell launching Codex. Do not use
`curl -k`, disable TLS verification, or transfer the CA's private key.

In an **elevated** Windows shell, preview and then apply the firewall rule:

```powershell
scripts\api-firewall.ps1 -LanAddress 192.168.1.243 -InterfaceAlias Ethernet -ClientAddress 192.168.1.0/24 -WhatIf
scripts\api-firewall.ps1 -LanAddress 192.168.1.243 -InterfaceAlias Ethernet -ClientAddress 192.168.1.0/24
```

The script accepts private IPv4 addresses or /24 through /32 subnets and verifies the Windows
interface. It scopes access by interface, local IP, and client network across all profiles. Supply a PowerShell array for multiple clients. Review
existing broad inbound allow rules, including proxy installer rules: those can bypass a narrow
rule. Keep the firewall enabled and backend port 8765 closed to remote clients. No IPv6 listener
is configured. Do not add router forwarding, public tunnels, or cloud relays.

## Linux REST and Codex

Load the transferred token from its user-private file:

```bash
export LIBRARIAN_URL='https://hal5000.fios-router.home'
export LIBRARIAN_TOKEN="$(cat ~/.config/librarian/client.token)"

# Keep the secret out of curl's argument list.
printf 'Authorization: Bearer %s\n' "$LIBRARIAN_TOKEN" |
  curl --fail-with-body --header @- "$LIBRARIAN_URL/v1/status"

printf 'Authorization: Bearer %s\n' "$LIBRARIAN_TOKEN" |
  curl --fail-with-body --header @- \
    --header 'Content-Type: application/json' \
    --data '{"query":"Lipschitz continuity","k":5,"mode":"hybrid"}' \
    "$LIBRARIAN_URL/v1/search"
```

Wait until status reports `ready: true`. Fetch a returned `page_url` with the same header to
read and verify the citation. An unauthenticated request must return 401. A device excluded by
the firewall must not connect. Check local DNS/hosts for resolution errors and the hostname/CA
trust for certificate errors.

Add to `~/.codex/config.toml`, substituting the actual hostname:

```toml
[mcp_servers.librarian]
url = "https://hal5000.fios-router.home/mcp"
bearer_token_env_var = "LIBRARIAN_TOKEN"
tool_timeout_sec = 75
```

Start Codex from the shell with the token exported. Store only the environment variable name
in Codex configuration. Ask it to search, read the best matching pages, and cite printed page,
PDF page, and lines. See [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## Automatic startup

Use the [WinSW service installer](windows-services.md#install). It replaces the earlier
scheduled-task approach. The selected client network is `192.168.1.0/24`; bearer tokens remain
required. Native services run under separate passwordless virtual service accounts.

After indexing changes, restart the API to refresh cached pages, vocabulary, and vectors.
Check readiness, a search-to-page round trip, and client certificate trust after deployment.
