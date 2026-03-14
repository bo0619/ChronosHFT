function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $fullPath = Join-Path (Get-Location) $Path
    [System.IO.File]::WriteAllText($fullPath, $Content, [System.Text.UTF8Encoding]::new($false))
}

function Replace-Exact([string]$Path, [string]$Old, [string]$New) {
    $content = Get-Content $Path -Raw
    if (-not $content.Contains($Old)) {
        throw "Snippet not found in $Path"
    }
    Write-Utf8NoBom $Path ($content.Replace($Old, $New))
}

$stubOld = @"
if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub
"@

$stubNew = @"
if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    requests_stub.Session = lambda *args, **kwargs: None
    requests_stub.Request = object
    sys.modules["requests"] = requests_stub
"@

$files = @(
    'tests/test_strategy_oms_coordination.py',
    'tests/test_oms_risk_wiring.py',
    'tests/test_oms_reconcile_backoff.py',
    'tests/test_dashboard_balances.py',
    'tests/test_ml_sniper_adaptation.py',
    'tests/test_dashboard_v2.py'
)

foreach ($file in $files) {
    $content = Get-Content $file -Raw
    if ($content.Contains($stubOld)) {
        Write-Utf8NoBom $file ($content.Replace($stubOld, $stubNew))
    }
}

Replace-Exact 'tests/test_exchange_truth_and_risk.py' @"
from event.type import (
    AccountData,
    Event,
    ExchangeAccountUpdate,
    ExchangeOrderUpdate,
    ExecutionPolicy,
    MarkPriceData,
    OrderBook,
    OrderIntent,
    Side,
    EVENT_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ACCOUNT_UPDATE,
    EVENT_EXCHANGE_ORDER_UPDATE,`r`n    EVENT_SYSTEM_HEALTH,`r`n    GatewayState,`r`n)
"@ @"
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
"@
