# DA_CR1_bot
import os
import logging
from telegram.ext import ApplicationBuilder, CommandHandler

# Логирование
logging.basicConfig(level=logging.INFO)

async def start(update, context):
    await update.message.reply_text("Бот запущен! Я готов искать сигналы по стратегии.")

def main():
    token = os.getenv("BOT_TOKEN")  # токен возьмём из Render
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))

    app.run_polling()

if __name__ == "__main__":
    main()
