@echo off
cd /d C:\Users\FAHAD\Downloads\RTS

echo ----------------------------------------
echo 🔄 رفع المشروع الى GitHub (بدون database)...
echo ----------------------------------------

REM تأكد أن .gitignore يمنع قاعدة البيانات
echo database/ > .gitignore
echo *.db >> .gitignore
echo __pycache__/ >> .gitignore
echo *.pyc >> .gitignore
echo venv/ >> .gitignore
echo .env/ >> .gitignore

git init
git remote remove origin 2>nul
git remote add origin https://github.com/divisionfa-collab/web-game

git add .
git commit -m "Auto upload - exclude database"
git branch -M main
git push -u origin main

echo ----------------------------------------
echo ✅ تم رفع المشروع بنجاح (قاعدة البيانات غير مرفوعة)
echo ----------------------------------------
pause
