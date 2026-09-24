@echo off
rem Avvia il monitor scrivendo tutto in monitor.log. Lanciato (nascosto) da avvio_automatico.vbs
cd /d "C:\Users\Marco\Desktop\Errore prezzo"
"C:\Users\Marco\AppData\Local\Programs\Python\Python313\python.exe" -u price_alert.py >> monitor.log 2>&1
