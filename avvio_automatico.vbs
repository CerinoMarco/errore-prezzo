' ============================================================
' Avvio automatico del monitor errori di prezzo.
' Lancia avvia.bat in BACKGROUND (nessuna finestra). Il log finisce in monitor.log
'
' PER DISATTIVARE l'avvio automatico: elimina la COPIA di questo file da:
'   C:\Users\Marco\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup
'
' PER FERMARLO subito: Gestione attivita' (Ctrl+Shift+Esc) -> python.exe -> Termina.
' ============================================================
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c ""C:\Users\Marco\Desktop\Errore prezzo\avvia.bat""", 0, False
