@echo off
cd /d C:\Users\FAHAD\Downloads\RTS

echo ----------------------------------------
echo 🔄 رفع المشروع الى GitHub (بدون database)...
echo ----------------------------------------

REM اعداد .gitignore للتأكد
echo database/ > .gitignore
echo *.db >> .gitignore
echo __pycache__/ >> .gitignore
echo *.pyc >> .gitignore
echo venv/ >> .gitignore
echo .env/ >> .gitignore

REM تهيئة git والربط بالمستودع
git init
git remote remove origin 2>nul
git remote add origin https://github.com/divisionfa-collab/web-game

REM جلب التغييرات من GitHub للدمج
git pull origin main --allow-unrelated-histories

REM حذف قاعدة البيانات من التتبع (تظل عندك محلياً)
git rm -r --cached database 2>nul
git rm --cached *.db 2>nul

REM رفع باقي الملفات
git add .
git commit -m "Auto upload - exclude database"
git branch -M main
git push -u origin main

echo ----------------------------------------
echo ✅ تم رفع المشروع بنجاح (قاعدة البيانات غير موجودة في GitHub)
echo ----------------------------------------
pause
