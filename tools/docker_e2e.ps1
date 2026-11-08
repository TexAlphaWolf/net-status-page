# End to end inside the image, from Windows PowerShell: a web server in another container is checked, goes away, and
# the status and the page are read from the same volume (read-only root, no capabilities). Needs Docker Desktop.
# Usage: .\tools\docker_e2e.ps1 [-Image upstatus]
param([string]$Image = "upstatus")
$ErrorActionPreference = "Stop"

$id = Get-Random
$vol = "upstatus-e2e-$id"
$net = "upstatus-e2e-net-$id"
$srv = "upstatus-e2e-srv-$id"
$secure = @("--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp", "--network", $net, "-v", "${vol}:/data")

# Windows PowerShell 5.1 turns a program's message on stderr into an error under "Stop": the commands that are
# expected to fail run with errors not terminating, and only their exit code is looked at.
function Invoke-UsCode([int[]]$Allowed, [string[]]$UsArgs) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = docker run --rm @secure $Image @UsArgs 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($Allowed -notcontains $code) { throw "upstatus $UsArgs exited with $code : $out" }
    ($out | Out-String)
}
function Invoke-Us { Invoke-UsCode @(0) $args }
function Read-Volume($path) {
    $out = docker run --rm --entrypoint cat -v "${vol}:/data" $Image $path
    if ($LASTEXITCODE -ne 0) { throw "cannot read $path" }
    ($out | Out-String)
}
function Assert-Contains($text, $needle) { if ($text -notmatch [regex]::Escape($needle)) { throw "expected '$needle' in: $text" } }
function Assert-Missing($text, $needle) { if ($text -match [regex]::Escape($needle)) { throw "did not expect '$needle' in: $text" } }

try {
    docker network create $net | Out-Null
    docker volume create $vol | Out-Null
    docker run --rm --user 0 -v "${vol}:/data" --entrypoint chown $Image 10001 /data
    if ($LASTEXITCODE -ne 0) { throw "could not prepare the volume" }
    docker run -d --name $srv --network $net --entrypoint python $Image -m http.server 8000 --directory /tmp | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not start the test server" }
    Start-Sleep -Seconds 2

    $conf = @"
[site]
title = "E2E status"

[defaults]
fail_after = 2
recover_after = 1

[[check]]
name = "Web"
group = "Test"
type = "http"
url = "http://${srv}:8000/"
contains = "Directory listing"

[[check]]
name = "Port"
group = "Test"
type = "tcp"
host = "$srv"
port = 8000

[[check]]
name = "Resolver"
group = "Test"
type = "dns"
host = "localhost"
"@
    $conf | docker run --rm -i --user 0 -v "${vol}:/data" --entrypoint sh $Image -c "cat > /data/upstatus.toml && chmod 644 /data/upstatus.toml"
    if ($LASTEXITCODE -ne 0) { throw "could not write the configuration" }

    Assert-Contains (Invoke-Us validate) "ok: 3 check"
    Assert-Contains (Invoke-Us run) "ran 3 check"
    $status = Invoke-Us status
    foreach ($name in "Web", "Port", "Resolver") { Assert-Contains $status "${name}: up" }
    Assert-Contains (Invoke-Us run) "ran 0 check"
    $page = Read-Volume "/data/public/index.html"
    Assert-Contains $page "All systems operational"
    Assert-Missing $page $srv
    Assert-Missing $page ":8000"
    Assert-Missing $page "<script"

    docker rm -f $srv | Out-Null
    Invoke-UsCode @(0, 1) @("run", "--all") | Out-Null
    Invoke-UsCode @(1) @("run", "--all") | Out-Null
    $status = Invoke-UsCode @(1) @("status")
    Assert-Contains $status "Web: DOWN"
    Assert-Contains $status "Resolver: up"
    Assert-Contains (Invoke-Us incidents) "Web: still going"
    Assert-Contains (Read-Volume "/data/public/index.html") "Partial outage"
    Invoke-UsCode @(2) @("--config", "/nonexistent.toml", "validate") | Out-Null
    Write-Host "docker end-to-end: OK"
}
finally {
    $ErrorActionPreference = "Continue"
    docker rm -f $srv 2>&1 | Out-Null
    docker volume rm -f $vol 2>&1 | Out-Null
    docker network rm $net 2>&1 | Out-Null
}