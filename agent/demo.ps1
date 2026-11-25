param(
    [string]$Python = 'python',
    [string]$Server = (Join-Path $PSScriptRoot '../services/context/synapse_auto_server.py'),
    [switch]$TwoSystems,
    [switch]$Weekly,
    [switch]$GoogleCalendar,
    [switch]$LiveTest,
    [string]$TasksServer = (Join-Path $PSScriptRoot '../services/tasks/synapse_auto_server.py'),
    [string]$Earliest = '2026-11-23T17:00:00+05:30',
    [ValidateSet('calendar', 'task')][string]$Action,
    [string]$Request
)

$ErrorActionPreference = 'Stop'
$pythonPath = (Get-Command $Python -CommandType Application).Source.Replace('\', '/')
if (($GoogleCalendar -or $LiveTest) -and ($Weekly -or $TwoSystems -or $Action -ne 'calendar')) {
    throw 'Google Calendar mode requires -Action calendar.'
}
if ($Weekly) {
    if (-not $Request) { throw 'Weekly planning requires -Request.' }
    $weeklyContext = (Resolve-Path -LiteralPath $Server).Path
    $weeklyTasks = (Resolve-Path -LiteralPath $TasksServer).Path
    Push-Location (Join-Path $PSScriptRoot '..')
    try {
        & $pythonPath -m agent.weekly --request $Request --earliest $Earliest --context $weeklyContext --tasks $weeklyTasks
        exit $LASTEXITCODE
    } finally {
        Pop-Location
    }
}
if ($Action) {
    if (-not $Request) { throw 'An action requires -Request.' }
    $contextPath = (Resolve-Path -LiteralPath $Server).Path
    $actionTasksPath = (Resolve-Path -LiteralPath $TasksServer).Path
    Push-Location (Join-Path $PSScriptRoot '..')
    try {
        $actionFlags = @()
        if ($GoogleCalendar) { $actionFlags += '--google' }
        if ($LiveTest) { $actionFlags += '--live-test' }
        & $pythonPath -m agent.actions $Action --request $Request --earliest $Earliest --context $contextPath --tasks $actionTasksPath @actionFlags
        exit $LASTEXITCODE
    } finally {
        Pop-Location
    }
}
$serverPath = (Resolve-Path -LiteralPath $Server).Path.Replace('\', '/')

# Pass the stdio configuration emitted by Synapse as a one-run override.
$mcpConfig = 'mcp_servers.context={command="' + $pythonPath + '",args=["' + $serverPath + '"],env={ENDPOINT_BASE_URL="http://127.0.0.1:8000"},required=true,enabled_tools=["list_commitments"],tools={list_commitments={approval_mode="approve"}}}'
    $prompt = 'Use the context MCP tool list_commitments to answer: What time does my design review end in IST? Use only the MCP tool for evidence. Do not use shell commands, files or direct HTTP. If the tool fails, report the failure.'

if ($TwoSystems) {
    $tasksPath = (Resolve-Path -LiteralPath $TasksServer).Path.Replace('\', '/')
    $tasksConfig = 'mcp_servers.tasks={command="' + $pythonPath + '",args=["' + $tasksPath + '"],env={ENDPOINT_BASE_URL="http://127.0.0.1:8001"},required=true,enabled_tools=["list_tasks","list_routines"],tools={list_tasks={approval_mode="approve"},list_routines={approval_mode="approve"}}}'
    $schema = Join-Path $PSScriptRoot 'task-selection.schema.json'
    if (-not $Request) { $Request = 'Find me a free 90-minute slot before my hackathon submission deadline so I can finish my README.' }
    $prompt = "User request: $Request`nCall tasks/list_tasks and context/list_commitments to retrieve the source data. Select the pending task that matches the request. Return its ID in task_id for the deterministic scheduler to compute and explain the slot. If no task matches, return an empty task_id. Use only those two MCP tools. Do not use shell commands, files, or direct HTTP."

    $trace = @(codex exec --skip-git-repo-check --ephemeral --ignore-user-config --sandbox read-only --json -c $mcpConfig -c $tasksConfig --output-schema $schema $prompt)
    $codexExit = $LASTEXITCODE
    $trace | Write-Output
    if ($codexExit -ne 0) { exit $codexExit }
    $trace | & $pythonPath (Join-Path $PSScriptRoot 'schedule.py') --earliest $Earliest
    exit $LASTEXITCODE
}

codex exec --skip-git-repo-check --ephemeral --ignore-user-config --sandbox read-only --json -c $mcpConfig $prompt
exit $LASTEXITCODE
