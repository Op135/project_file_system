param(
    [string]$StoragePath = "",
    [string]$ProjectSummaryPath = "",
    [switch]$Apply,
    [switch]$AdminConfirm,
    [int]$PreviewLimit = 80
)

$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "This script requires PowerShell 7+ because storage-general.json contains empty-string property names."
}

if (-not $StoragePath) {
    $StoragePath = Join-Path $PSScriptRoot "..\.nicegui\storage-general.json"
}

if (-not $ProjectSummaryPath) {
    $ProjectSummaryPath = Join-Path $PSScriptRoot "..\data\project_summary.json"
}

$resolvedStorage = Resolve-Path -LiteralPath $StoragePath
$storageFile = $resolvedStorage.Path
$resolvedProjectSummary = Resolve-Path -LiteralPath $ProjectSummaryPath
$projectSummaryFile = $resolvedProjectSummary.Path

if ($Apply -and -not $AdminConfirm) {
    throw "Apply mode requires -AdminConfirm. Dry-run mode does not modify files."
}

if ($Apply) {
    Write-Warning "Stop the NiceGUI service before applying, otherwise in-memory storage can overwrite storage-general.json later."
}

$sourceStates = @("试产", "量产")
$targetState = "转产"

$storage = Get-Content -LiteralPath $storageFile -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable
$projectSummaryJson = Get-Content -LiteralPath $projectSummaryFile -Raw -Encoding UTF8 | ConvertFrom-Json -AsHashtable

if (-not $storage.ContainsKey("project_summary")) {
    throw "Missing project_summary in $storageFile"
}

if (-not $storage.ContainsKey("project_req_max_ver")) {
    throw "Missing project_req_max_ver in $storageFile"
}

if (-not $projectSummaryJson) {
    throw "Empty or invalid project summary file: $projectSummaryFile"
}

$storageProjectSummary = $storage["project_summary"]
$projectReqMaxVer = $storage["project_req_max_ver"]
$storageProjectSummaryKeys = @($storageProjectSummary.Keys)
$projectSummaryJsonKeys = @($projectSummaryJson.Keys)
$projectReqKeys = @($projectReqMaxVer.Keys)

$projectsWithRequirement = @{}
foreach ($projectName in $projectReqKeys) {
    $projectsWithRequirement[$projectName] = $true
}

function Update-ProjectStates {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$ProjectSummary,
        [Parameter(Mandatory = $true)]
        [string]$SourceName,
        [Parameter(Mandatory = $true)]
        [hashtable]$ProjectsWithRequirement,
        [Parameter(Mandatory = $true)]
        [string[]]$SourceStates,
        [Parameter(Mandatory = $true)]
        [string]$TargetState,
        [Parameter(Mandatory = $true)]
        [bool]$ApplyChanges
    )

    $affected = @()
    foreach ($projectName in @($ProjectSummary.Keys)) {
        $projectData = $ProjectSummary[$projectName]

        if (-not ($projectData -is [hashtable]) -or -not $projectData.ContainsKey("state")) {
            continue
        }

        $currentState = ([string]$projectData["state"]).Trim()
        if (($SourceStates -contains $currentState) -and -not $ProjectsWithRequirement.ContainsKey($projectName)) {
            $affected += [PSCustomObject]@{
                Source = $SourceName
                Project = $projectName
                OldState = $currentState
                NewState = $TargetState
            }

            if ($ApplyChanges) {
                $projectData["state"] = $TargetState
            }
        }
    }

    return $affected
}

$storageAffectedProjects = Update-ProjectStates `
    -ProjectSummary $storageProjectSummary `
    -SourceName "storage-general" `
    -ProjectsWithRequirement $projectsWithRequirement `
    -SourceStates $sourceStates `
    -TargetState $targetState `
    -ApplyChanges $Apply.IsPresent

$jsonAffectedProjects = Update-ProjectStates `
    -ProjectSummary $projectSummaryJson `
    -SourceName "data/project_summary" `
    -ProjectsWithRequirement $projectsWithRequirement `
    -SourceStates $sourceStates `
    -TargetState $targetState `
    -ApplyChanges $Apply.IsPresent

$affectedProjects = @($storageAffectedProjects) + @($jsonAffectedProjects)
$storageMatchedNames = @($storageAffectedProjects | ForEach-Object { $_.Project })
$jsonMatchedNames = @($jsonAffectedProjects | ForEach-Object { $_.Project })
$storageOnlyNames = @($storageMatchedNames | Where-Object { $jsonMatchedNames -notcontains $_ })
$jsonOnlyNames = @($jsonMatchedNames | Where-Object { $storageMatchedNames -notcontains $_ })

Write-Host "Storage file: $storageFile"
Write-Host "Data project summary file: $projectSummaryFile"
Write-Host "storage project_summary count: $($storageProjectSummaryKeys.Count)"
Write-Host "data project_summary count: $($projectSummaryJsonKeys.Count)"
Write-Host "project_req_max_ver count: $($projectReqKeys.Count)"
Write-Host "storage matched projects: $($storageAffectedProjects.Count)"
Write-Host "data matched projects: $($jsonAffectedProjects.Count)"
if ($storageOnlyNames.Count -eq 0 -and $jsonOnlyNames.Count -eq 0) {
    Write-Host "matched project-name sets: identical"
} else {
    Write-Warning "matched project-name sets differ. storage-only=$($storageOnlyNames.Count), data-only=$($jsonOnlyNames.Count)"
}

function Show-AffectedPreview {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Title,
        [Parameter(Mandatory = $true)]
        [object[]]$Projects,
        [Parameter(Mandatory = $true)]
        [int]$Limit
    )

    if ($Projects.Count -eq 0) {
        return
    }

    Write-Host ""
    Write-Host "$Title preview:"
    $Projects |
        Select-Object -First $Limit |
        Format-Table -AutoSize

    if ($Projects.Count -gt $Limit) {
        Write-Host "Preview truncated. Increase -PreviewLimit to see more rows."
    }
}

Show-AffectedPreview -Title "storage-general" -Projects $storageAffectedProjects -Limit $PreviewLimit
Show-AffectedPreview -Title "data/project_summary" -Projects $jsonAffectedProjects -Limit $PreviewLimit

if (-not $Apply) {
    Write-Host "Dry run only. Re-run with -Apply -AdminConfirm to write storage-general.json and data/project_summary.json."
    exit 0
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$storageBackupPath = "$storageFile.bak-$timestamp"
$projectSummaryBackupPath = "$projectSummaryFile.bak-$timestamp"
Copy-Item -LiteralPath $storageFile -Destination $storageBackupPath -Force
Copy-Item -LiteralPath $projectSummaryFile -Destination $projectSummaryBackupPath -Force

$storageJson = $storage | ConvertTo-Json -Depth 100
$projectSummaryOutputJson = $projectSummaryJson | ConvertTo-Json -Depth 100
Set-Content -LiteralPath $storageFile -Value $storageJson -Encoding UTF8
Set-Content -LiteralPath $projectSummaryFile -Value $projectSummaryOutputJson -Encoding UTF8

Write-Host "Updated storage-general.json and data/project_summary.json."
Write-Host "Storage backup: $storageBackupPath"
Write-Host "Data project summary backup: $projectSummaryBackupPath"
