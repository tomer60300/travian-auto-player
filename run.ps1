# Barca account helper — sets env vars and runs travian CLI
$env:TRAVIAN_USERNAME='chetrit1311@gmail.com'
$env:TRAVIAN_PASSWORD='Password1'
$env:TRAVIAN_BASE_URL='https://ts2.x1.europe.travian.com'
Set-Location C:\Users\tomerc\.openclaw\workspace\travian-autoS
python -m travian_api @args
