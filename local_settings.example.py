"""Образец локальных настроек: скопируй в local_settings.py и впиши свой контакт.

trackmania.io не требует авторизации, но просит вежливый User-Agent с контактом
(например, ник в Discord), чтобы можно было связаться при проблемах.
Скопировать: cp local_settings.example.py local_settings.py
local_settings.py добавлен в .gitignore и в репозиторий не попадёт.
Можно не создавать файл, а задать переменную окружения TMIO_USER_AGENT.
"""

USER_AGENT = ""
