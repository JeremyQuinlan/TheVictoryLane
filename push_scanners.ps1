# push_scanners.ps1
$date = Get-Date -Format 'yyyyMMdd'
$src  = "C:\Users\jerem\Documents\TradeIdeasPro"
$dst  = "C:\Tools\TheVictoryLane\scanners"

New-Item -ItemType Directory -Force -Path $dst | Out-Null

$files = Get-ChildItem "$src\alertlogging.*.$date.csv" -ErrorAction SilentlyContinue
if ($files.Count -eq 0) {
    Write-Host "No CSV files found for $date in $src"
    exit 1
}
$files | Copy-Item -Destination $dst -Force
Write-Host "Copied $($files.Count) scanner CSV(s) to scanners/"

Set-Location "C:\Tools\TheVictoryLane"
git add scanners/
git commit -m "Scanners $date $(Get-Date -Format 'HH:mm') ET"
git push

Write-Host "Done - GitHub Actions will build the page now."