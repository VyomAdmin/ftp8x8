Set-StrictMode -Version Latest
Set-Location 'C:\Users\UpdeshSingh\ftp8x8'

$project = 'psychic-lens-456414-e4'
$saName = 'ftp8x8-drive-uploader'
$saEmail = "$saName@$project.iam.gserviceaccount.com"
$keyFile = 'drive_sa_key.json'

Write-Output "Using project: $project"
Write-Output "Service account: $saEmail"

gcloud config set project $project

Write-Output 'Enabling Google Drive API...'
gcloud services enable drive.googleapis.com --project $project

$exists = $false
try {
    gcloud iam service-accounts describe $saEmail --project $project | Out-Null
    $exists = $true
} catch {
    $exists = $false
}

if ($exists) {
    Write-Output "Service account $saEmail already exists."
} else {
    Write-Output "Creating service account $saEmail..."
    gcloud iam service-accounts create $saName --display-name='ftp8x8 Drive uploader' --project $project
}

if (Test-Path $keyFile) {
    Remove-Item $keyFile -Force
}

Write-Output "Creating service account key file $keyFile..."
gcloud iam service-accounts keys create $keyFile --iam-account $saEmail --project $project

Write-Output "Service account email: $saEmail"
