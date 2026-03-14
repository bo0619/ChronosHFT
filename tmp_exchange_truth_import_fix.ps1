function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $fullPath = Join-Path (Get-Location) $Path
    [System.IO.File]::WriteAllText($fullPath, $Content, [System.Text.UTF8Encoding]::new($false))
}

$content = Get-Content 'tests/test_exchange_truth_and_risk.py' -Raw
$pattern = 'from event\.type import \((?s).*?\)\r?\nfrom gateway\.binance\.gateway import BinanceGateway'
$replacement = @"
from event.type import (
    AccountData,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    ExecutionPolicy,
    GatewayState,
    MarkPriceData,
    OrderBook,
    OrderIntent,
    Side,
    EVENT_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,
    EVENT_SYSTEM_HEALTH,
)
from gateway.binance.gateway import BinanceGateway
"@
$newContent = [regex]::Replace($content, $pattern, $replacement, 1)
if ($newContent -eq $content) { throw 'Import block not replaced' }
Write-Utf8NoBom 'tests/test_exchange_truth_and_risk.py' $newContent
