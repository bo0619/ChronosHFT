function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $fullPath = Join-Path (Get-Location) $Path
    [System.IO.File]::WriteAllText($fullPath, $Content, [System.Text.UTF8Encoding]::new($false))
}

$content = Get-Content 'gateway/binance/gateway.py' -Raw
$old = @"
        self.active = False
        if self.ws:
            self.ws.close()
        if self.state != GatewayState.ERROR:
"@
$new = @"
        self.active = False
        ws_client = getattr(self, "ws", None)
        if ws_client:
            ws_client.close()
        if self.state != GatewayState.ERROR:
"@
if (-not $content.Contains($old)) { throw 'Gateway fault block not found' }
Write-Utf8NoBom 'gateway/binance/gateway.py' ($content.Replace($old, $new))
