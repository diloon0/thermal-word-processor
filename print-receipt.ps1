# Print text to the Sunydog / MPT-II 58mm thermal printer over Bluetooth (ESC/POS)
# Usage:
#   .\print-receipt.ps1                       # prints a test receipt
#   .\print-receipt.ps1 -Text "Hello`nWorld"  # prints your own text
#   .\print-receipt.ps1 -Title "Groceries" -Text "milk`neggs"
#   .\print-receipt.ps1 -Feed 8               # more blank space before the tear edge
param(
    [string]$Port = 'COM7',
    [string]$Title,
    [string]$Text,
    [int]$Feed = 3    # blank lines after the text so it clears the tear bar
)

$ESC = [char]0x1B; $GS = [char]0x1D
$init       = "$ESC@"
$center     = "${ESC}a$([char]1)"
$left       = "${ESC}a$([char]0)"
$boldOn     = "${ESC}E$([char]1)"
$boldOff    = "${ESC}E$([char]0)"
$bigOn      = "${GS}!$([char]0x11)"   # double width + height
$bigOff     = "${GS}!$([char]0)"
$tailFeed   = "`n" * $Feed

if (-not $Title -and -not $Text) {
    $Title = 'BLUETOOTH OK'
    $Text = @"
Printer : MPT-II (Sunydog 58mm)
Link    : Bluetooth $Port
Sent    : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
--------------------------------
No manufacturer app needed.
"@
}

$job = $init
if ($Title) { $job += "$center$boldOn$bigOn$Title`n$bigOff$boldOff`n" }
if ($Text)  { $job += "$left$($Text -replace "`r`n", "`n")`n" }
$job += $tailFeed

$sp = New-Object System.IO.Ports.SerialPort $Port, 9600
$sp.Encoding = [System.Text.Encoding]::GetEncoding(437)
$sp.WriteTimeout = 10000
try {
    $sp.Open()
    $sp.Write($job)
    Start-Sleep -Milliseconds 500   # let the Bluetooth link flush before closing
    "Sent $($job.Length) bytes to $Port."
} finally {
    if ($sp.IsOpen) { $sp.Close() }
}
