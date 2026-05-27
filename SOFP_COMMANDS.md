# SoFP Extractor Commands

Run these commands in PowerShell.

## 1. Run The First Company

```powershell
cd "E:\AMBEON\script"
$env:PYTHONIOENCODING='utf-8'
python "New folder\sofp_extractor.py" "reports\company1\Annual\1072_1749226867867.pdf" --company company1 --out "json_logs\company1_SoFP.json"
```

## 2. Run All Companies

```powershell
cd "E:\AMBEON\script"
$env:PYTHONIOENCODING='utf-8'

Get-ChildItem "reports" -Recurse -Filter "*.pdf" |
  Where-Object { $_.FullName -like "*\Annual\*" } |
  ForEach-Object {
    $company = $_.Directory.Parent.Name
    python "New folder\sofp_extractor.py" $_.FullName --company $company --out "json_logs\$($company)_SoFP.json"
  }
```

## 3. See The Web Table For Company 1

```powershell
cd "E:\AMBEON\script"
python "New folder\view_sofp.py" "json_logs\company1_SoFP.json"
```

## 4. Run The Web Table For All Companies

```powershell
cd "E:\AMBEON\script"

Get-ChildItem "json_logs" -Filter "*_SoFP.json" |
  ForEach-Object {
    python "New folder\view_sofp.py" $_.FullName --no-browser
    $html = Join-Path "json_logs" "$($_.BaseName).html"
    Copy-Item "$env:TEMP\sofp_viewer.html" $html -Force
    Start-Process (Resolve-Path $html)
  }
```
