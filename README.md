# בוט טלגרם להחלפת פנים, ב-Python, שרץ בטלפון (Termux)

## שלב 1 – יצירת בוט בטלגרם
1. בטלגרם, פתח צ'אט עם **@BotFather**
2. שלח `/newbot`, תן שם ושם משתמש (חייב להסתיים ב-`bot`)
3. תקבל **טוקן** – שמור אותו ואל תשתף אותו עם אף אחד

## שלב 2 – התקנת Termux
התקן את Termux מ-**F-Droid** (הגרסה ב-Google Play ישנה ולא עובדת טוב).

## שלב 3 – התקנה ב-Termux
```bash
pkg update && pkg upgrade -y
pkg install python git python-numpy python-onnxruntime python-scipy python-pillow -y
git clone https://github.com/benny054727/HTML.git bot
cd bot
pip install -r requirements.txt
python download_models.py
```

`download_models.py` מוריד כ-1.6GB של מודלים לתיקייה `models/` ובודק שהם תקינים.

## שלב 4 – הגדרת הטוקן
```bash
cp .env.example .env
nano .env
```
החלף את הטוקן לדוגמה בטוקן שקיבלת. שמירה: `Ctrl+O` ואז `Enter`, יציאה: `Ctrl+X`.

## שלב 5 – הרצה
```bash
python bot.py
```
או, כדי שיופעל מחדש אוטומטית אם נופל:
```bash
./run.sh
```

## טיפים כדי שהבוט ימשיך לרוץ
- בהתראה של Termux לחץ **Acquire wakelock** (או ש-`run.sh` עושה את זה).
- בהגדרות אנדרואיד: בטל **אופטימיזציית סוללה** ל-Termux.
- הבוט רץ רק כשהטלפון דלוק ומחובר לאינטרנט.

## פקודות הבוט
| פקודה | מה עושה |
|---|---|
| `/start` | הודעת פתיחה |
| `/help` | רשימת פקודות |
| `/time` | השעה עכשיו |
| `/id` | ה-ID שלך |
| `/swap` | החלפת פנים: תמונה מקורית ואז תמונת יעד |
| `/cancel` | ביטול החלפה באמצע |
| `/enhance` | הדלקה/כיבוי של חידוד הפנים (GFPGAN) |
| טקסט רגיל | הבוט מחזיר אותו |

## החלפת פנים
1. שולחים `/swap`
2. שולחים **תמונה מקורית** - ממנה נלקחות הפנים (הפנים הגדולות בתמונה)
3. שולחים **תמונת יעד** - הפנים הגדולות בה מוחלפות

⚠️ השתמש רק בתמונות של אנשים שהסכימו לכך.

ההחלפה נעשית בשני שלבים: inswapper (מוחק משקפיים ודברים שמכסים את הפנים) ואז HyperSwap (איכות ודמיון). אחרי ההחלפה הפנים עוברות חידוד עם GFPGAN (אפשר לכבות עם `/enhance`).

המודלים (InsightFace: SCRFD, ArcFace, inswapper_128; FaceFusion: HyperSwap 1a; GFPGAN) מותרים לשימוש אישי ולא מסחרי בלבד (HyperSwap: רישיון ResearchRAIL).
