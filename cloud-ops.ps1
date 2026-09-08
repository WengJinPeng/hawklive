param(
    [ValidateSet("Start", "Stop", "Status", "Backup", "Logs")]
    [string]$Action = "Status"
)

$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$composeFile = Join-Path $projectDirectory "docker-compose.cloud.yml"

Push-Location $projectDirectory
try {
    switch ($Action) {
        "Start" {
            docker compose -f $composeFile up -d --build
        }
        "Stop" {
            docker compose -f $composeFile stop
        }
        "Status" {
            docker compose -f $composeFile ps
            try {
                docker compose -f $composeFile exec -T ingest-api python -c `
                    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5).read().decode())"
            }
            catch {
                Write-Error "HawkHive API health check failed: $($_.Exception.Message)"
                exit 1
            }
        }
        "Backup" {
            docker compose -f $composeFile exec -T backup-runner python cloud_backup.py `
                --daily-dir /app/backups/daily --monthly-dir /app/monthly-backups
        }
        "Logs" {
            docker compose -f $composeFile logs --tail 100
        }
    }
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
