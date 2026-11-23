"""Validate the Windows deployment contract without touching SCM or firewall state."""
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows deployment")
REPO = Path(__file__).resolve().parents[1]
PS = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")


def test_service_configs_have_isolated_accounts_and_direct_processes(tmp_path):
    output = tmp_path / "services & config"
    state = tmp_path / "private state"
    subprocess.run([PS, "-NoProfile", "-File", str(REPO / "scripts/api-service-config.ps1"),
                    "-OutputDirectory", str(output), "-StateDirectory", str(state),
                    "-CaddyExe", str(tmp_path / "caddy.exe"), "-AuthMode", "Bearer"], check=True, capture_output=True)
    api = ET.parse(output / "LibrarianApi.xml").getroot()
    proxy = ET.parse(output / "LibrarianHttps.xml").getroot()
    for root in (api, proxy):
        assert root.findtext("serviceaccount/domain") == "NT SERVICE"
        assert root.findtext("serviceaccount/user") == root.findtext("id")
        assert root.find("serviceaccount/password") is None
        assert root.findtext("startmode") == "Automatic"
        assert root.find("onfailure").attrib["action"] == "restart"
        assert "powershell" not in root.findtext("executable").lower()
    assert api.findtext("executable").endswith("python.exe")
    assert "auth\\tokens.json" in api.findtext("arguments")
    assert "https://hal5000.fios-router.home" in api.findtext("arguments")
    assert proxy.findtext("depend") == "LibrarianApi"
    env = {e.attrib["name"]: e.attrib["value"] for e in proxy.findall("env")}
    assert env["LIBRARIAN_CLIENT_NETWORK"] == "192.168.1.0/24"
    assert env["LIBRARIAN_LAN_IP"] == "192.168.1.243"
    assert "@outside not remote_ip" in (output / "Caddyfile").read_text()


def test_firewall_accepts_private_subnet_and_rejects_broad_ranges():
    script = str(REPO / "scripts/api-firewall.ps1").replace("'", "''")
    mock = """
    $ErrorActionPreference = 'Stop'
    function Get-NetIPAddress { param($InterfaceAlias,$AddressFamily) [pscustomobject]@{IPAddress='192.168.1.243'} }
    function Get-NetFirewallRule { param($Name,$ErrorAction) $null }
    function New-NetFirewallRule {
      param($Name,$DisplayName,$Direction,$Action,$Protocol,$LocalPort,$LocalAddress,$RemoteAddress,$InterfaceAlias,$Profile)
      if ($LocalAddress -ne '192.168.1.243' -or $RemoteAddress -ne '192.168.1.0/24' -or $InterfaceAlias -ne 'Ethernet' -or $LocalPort -ne 443) { throw 'Incorrect scope' }
    }
    """
    for network, success in (("192.168.1.0/24", True), ("0.0.0.0/0", False), ("192.168.0.0/16", False)):
        command = mock + f"& '{script}' -LanAddress 192.168.1.243 -InterfaceAlias Ethernet -ClientAddress '{network}'"
        result = subprocess.run([PS, "-NoProfile", "-Command", command], capture_output=True)
        assert (result.returncode == 0) == success, result.stderr.decode(errors="replace")
