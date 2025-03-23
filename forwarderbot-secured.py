#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Media Forwarder Bot
Бот для автоматической пересылки медиафайлов из одного чата в другой
"""

import asyncio
import json
import logging
import os
from typing import Dict, List, Optional, Set, Tuple, Union, Any

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    InputPeerSelf, 
    MessageMediaPhoto, 
    MessageMediaDocument,
    Message
)

from aiogram import Bot, Dispatcher, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    CallbackQuery,
    Message as AiogramMessage
)

# Конфигурация
BOT_TOKEN = 'ВАШ ТОКЕН'
API_ID = 'ВАШ API_ID'
API_HASH = 'ВАШ API_HASH'
SESSION_NAME = 'media_forwarder'
DEFAULT_DELAY = 3  # Задержка по умолчанию (в секундах)
CONFIG_FILE = 'forwarder_config.json'
SAVED_MESSAGES_KEY = 'saved'  # Сокращенный ключ для callback_data

# ID администратора (только этот пользователь сможет использовать бота)
ADMIN_USER_ID = ВАШ ADMIN_USER_ID

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Состояния для FSM (Finite State Machine)
class ForwardingStates(StatesGroup):
    waiting_for_source = State()
    waiting_for_target = State()
    waiting_for_media_types = State()
    waiting_for_search = State()
    waiting_for_delay = State()
    waiting_for_limit = State()


class Configuration:
    """Класс для управления настройками бота"""
    
    def __init__(self, filename=CONFIG_FILE):
        self.filename = filename
        self.data = {
            'delay': DEFAULT_DELAY,
            'active_forwards': []
        }
        self.load()
    
    def load(self):
        """Загрузка конфигурации из файла"""
        try:
            if os.path.exists(self.filename):
                with open(self.filename, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                logger.info(f"Конфигурация загружена из {self.filename}")
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")
    
    def save(self):
        """Сохранение конфигурации в файл"""
        try:
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            logger.info(f"Конфигурация сохранена в {self.filename}")
        except Exception as e:
            logger.error(f"Ошибка сохранения конфигурации: {e}")
    
    def set_delay(self, delay: int):
        """Установка задержки между пересылками"""
        self.data['delay'] = delay
        self.save()
    
    def add_active_forward(self, source_id: Union[int, str], target_id: Union[int, str], media_types: List[str]):
        """Добавление новой активной пересылки"""
        # Удаляем дубликаты, если есть
        self.data['active_forwards'] = [
            fwd for fwd in self.data['active_forwards'] 
            if not(str(fwd['source_id']) == str(source_id) and str(fwd['target_id']) == str(target_id))
        ]
        
        self.data['active_forwards'].append({
            'source_id': str(source_id),
            'target_id': str(target_id),
            'media_types': list(media_types)
        })
        self.save()
    
    def remove_active_forward(self, source_id: Union[int, str], target_id: Union[int, str]) -> bool:
        """Удаление активной пересылки"""
        initial_length = len(self.data['active_forwards'])
        self.data['active_forwards'] = [
            fwd for fwd in self.data['active_forwards'] 
            if not(str(fwd['source_id']) == str(source_id) and str(fwd['target_id']) == str(target_id))
        ]
        if len(self.data['active_forwards']) < initial_length:
            self.save()
            return True
        return False
    
    def get_active_forwards(self) -> List[Dict]:
        """Получение списка активных пересылок"""
        return self.data['active_forwards']


class MediaForwarder:
    """Основной класс для пересылки медиафайлов"""
    
    def __init__(self, api_id: str, api_hash: str, session_name: str, config: Configuration):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.config = config
        self.client = None
        self.source_chat = None
        self.target_chat = None
        self.media_types: Set[str] = set()
        # Словарь активных пересылок {(source_id, target_id): {media_types, is_running}}
        self.active_forwards: Dict[Tuple[str, str], Dict] = {}
        # Счетчик пересланных сообщений
        self.message_count = 0
        # Словарь обработчиков событий {(source_id, target_id): handler}
        self.handlers: Dict[Tuple[str, str], Any] = {}
        # Чат с "Избранным" (Saved Messages)
        self.saved_messages = None
        # Словарь для отслеживания уже пересланных групп медиа
        self.forwarded_groups: Dict[Tuple[str, str], Set[int]] = {}
        
    async def connect(self):
        """Подключение к Telegram API"""
        self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        await self.client.start()
        logger.info("Клиент Telegram успешно подключен")
        
        # Получаем чат "Избранное" (Saved Messages)
        self.saved_messages = await self.client.get_entity(InputPeerSelf())
        
        # Восстанавливаем активные пересылки из конфигурации
        await self.restore_active_forwards()
        
    async def restore_active_forwards(self):
        """Восстанавливает активные пересылки из сохраненной конфигурации"""
        for forward in self.config.get_active_forwards():
            source_id = forward['source_id']
            target_id = forward['target_id']
            media_types = set(forward['media_types'])
            
            try:
                source_entity = await self.client.get_entity(int(source_id))
                
                # Проверяем, не является ли целью "Избранное"
                if str(target_id) == "saved_messages" or str(target_id) == SAVED_MESSAGES_KEY:
                    target_entity = self.saved_messages
                    target_id = "saved_messages"  # Стандартизируем ID
                else:
                    target_entity = await self.client.get_entity(int(target_id))
                
                # Запускаем отслеживание новых сообщений
                await self.start_forward_monitoring(source_entity, target_entity, media_types)
                logger.info(f"Восстановлена активная пересылка: {source_id} -> {target_id}")
            except Exception as e:
                logger.error(f"Не удалось восстановить пересылку {source_id} -> {target_id}: {e}")
    
    async def get_dialogs(self, offset=0, limit=10, query=None):
        """Получение списка диалогов с возможностью поиска и пагинации"""
        dialogs = []
        counter = 0
        
        # Используем запрос для фильтрации, если он указан
        async for dialog in self.client.iter_dialogs():
            if query and query.lower() not in dialog.name.lower():
                continue
                
            if counter >= offset:
                dialogs.append({
                    'id': dialog.id,
                    'name': dialog.name,
                    'type': 'channel' if dialog.is_channel else 
                           'group' if dialog.is_group else 'user'
                })
                
            counter += 1
            
            if len(dialogs) >= limit:
                break
                
        return dialogs, counter  # Возвращаем диалоги и общее количество
    
    async def set_source(self, chat_id):
        """Установка источника для пересылки"""
        try:
            self.source_chat = await self.client.get_entity(int(chat_id))
            return True, self.source_chat.title if hasattr(self.source_chat, 'title') else self.source_chat.first_name
        except Exception as e:
            logger.error(f"Ошибка при установке источника: {e}")
            return False, None
            
    async def set_target(self, chat_id):
        """Установка цели для пересылки"""
        try:
            if chat_id == "saved_messages" or chat_id == SAVED_MESSAGES_KEY:
                self.target_chat = self.saved_messages
                return True, "Избранное"
            else:
                self.target_chat = await self.client.get_entity(int(chat_id))
                return True, self.target_chat.title if hasattr(self.target_chat, 'title') else self.target_chat.first_name
        except Exception as e:
            logger.error(f"Ошибка при установке цели: {e}")
            return False, None

    def should_forward_message(self, message: Message) -> bool:
        """
        Проверяет, нужно ли пересылать сообщение в соответствии с заданными типами медиа
        """
        # Если список типов медиа пуст, пересылаем все сообщения с медиа
        if not self.media_types:
            return self.has_any_media(message)
        
        # Проверяем соответствие заданным типам медиа
        return self.check_media_type(message)
    
    def has_any_media(self, message: Message) -> bool:
        """Проверяет, содержит ли сообщение какой-либо медиафайл"""
        return bool(message.media) or bool(getattr(message, 'photo', None)) or bool(getattr(message, 'document', None)) or bool(getattr(message, 'video', None))
    
    def check_media_type(self, message: Message) -> bool:
        """Проверяет, соответствует ли тип медиа заданным критериям"""
        if not message.media and not getattr(message, 'photo', None) and not getattr(message, 'document', None) and not getattr(message, 'video', None):
            return False
            
        # Проверка на наличие фото
        if 'photo' in self.media_types:
            if getattr(message, 'photo', None):
                return True
            if hasattr(message, 'media') and isinstance(message.media, MessageMediaPhoto):
                return True
                
        # Проверка на наличие видео
        if 'video' in self.media_types:
            if getattr(message, 'video', None):
                return True
            if hasattr(message, 'media') and isinstance(message.media, MessageMediaDocument):
                if hasattr(message.media.document, 'mime_type') and message.media.document.mime_type:
                    if message.media.document.mime_type.startswith('video/'):
                        return True
                
        # Проверка на наличие документов
        if 'document' in self.media_types:
            if getattr(message, 'document', None):
                return True
            if hasattr(message, 'media') and isinstance(message.media, MessageMediaDocument):
                if hasattr(message.media.document, 'mime_type') and message.media.document.mime_type:
                    if not message.media.document.mime_type.startswith('video/'):
                        return True
        
        return False
    
    async def start_forward_monitoring(self, source_entity, target_entity, media_types):
        """Запускает отслеживание новых сообщений и их пересылку"""
        source_id = str(source_entity.id)
        target_id = "saved_messages" if target_entity.id == self.saved_messages.id else str(target_entity.id)
        key = (source_id, target_id)
        
        # Проверяем, не запущена ли уже такая пересылка
        if key in self.active_forwards and self.active_forwards[key]['is_running']:
            return False
        
        # Инициализируем словарь для отслеживания уже пересланных групп медиа
        self.forwarded_groups[key] = set()
        self.media_types = media_types
        
        # Создаем обработчик для новых сообщений
        async def handler(event):
            key_check = (source_id, target_id)
            if not self.active_forwards.get(key_check, {}).get('is_running', False):
                logger.info(f"Пересылка остановлена или не существует: {source_id} -> {target_id}")
                return
                
            message = event.message
            
            # Если это группа медиа, обрабатываем специальным образом
            if message.grouped_id:
                await self.process_media_group(message, target_entity, key_check)
            else:
                # Для одиночных сообщений проверяем тип медиа
                await self.process_single_message(message, target_entity, key_check)
        
        # Регистрируем обработчик
        event_handler = self.client.add_event_handler(handler, events.NewMessage(chats=source_entity))
        
        # Обновляем состояние
        self.active_forwards[key] = {
            'media_types': media_types,
            'is_running': True
        }
        
        # Сохраняем обработчик
        self.handlers[key] = event_handler
        
        # Сохраняем в конфигурацию
        self.config.add_active_forward(source_id, target_id, list(media_types))
        
        logger.info(f"Запущена пересылка: {source_id} -> {target_id}")
        return True
    
    async def process_media_group(self, message, target_entity, key):
        """
        Обрабатывает группу медиа (альбом)
        Исправлена версия: не использует параметр grouped_id в get_messages
        """
        # Если это группа медиа, проверяем, не пересылали ли мы уже эту группу
        if message.grouped_id in self.forwarded_groups[key]:
            # Эта группа уже была переслана или в процессе пересылки, пропускаем
            return
        
        # Отмечаем группу как обрабатываемую
        self.forwarded_groups[key].add(message.grouped_id)
        
        try:
            # Получаем последние сообщения и фильтруем по grouped_id
            # Важно: не используем параметр grouped_id в get_messages, так как он не поддерживается
            all_recent_messages = await self.client.get_messages(
                message.chat_id,
                limit=50  # Достаточно для альбома
            )
            
            # Фильтруем сообщения, чтобы получить только те, что входят в ту же группу
            group_messages = [
                msg for msg in all_recent_messages 
                if getattr(msg, 'grouped_id', None) == message.grouped_id
            ]
            
            # Если есть хотя бы одно сообщение с подходящим медиа, пересылаем всю группу
            has_matching_media = any(self.should_forward_message(msg) for msg in group_messages)
            
            if has_matching_media and group_messages:
                # Пересылаем группу сообщений
                await self.client.forward_messages(target_entity, group_messages)
                self.message_count += len(group_messages)
                
                delay = self.config.data['delay']
                logger.info(f"Переслана группа медиа ({len(group_messages)} элементов). Всего: {self.message_count}")
                await asyncio.sleep(delay)
        
        except FloodWaitError as e:
            logger.warning(f"Слишком много запросов, ждем {e.seconds} секунд")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Ошибка при обработке группы медиа: {e}")
        finally:
            # Ограничиваем размер списка пересланных групп
            if len(self.forwarded_groups[key]) > 100:
                # Удаляем самые старые записи, оставляя последние 50
                self.forwarded_groups[key] = set(list(self.forwarded_groups[key])[-50:])
    
    async def process_single_message(self, message, target_entity, key):
        """Обрабатывает одиночное сообщение"""
        # Проверяем, соответствует ли сообщение критериям
        should_forward = self.should_forward_message(message)
        
        if should_forward:
            try:
                # Пересылаем сообщение
                await self.client.forward_messages(target_entity, message)
                self.message_count += 1
                
                delay = self.config.data['delay']
                logger.info(f"Переслано сообщение #{self.message_count}. ID: {message.id}. Дата: {message.date}")
                await asyncio.sleep(delay)
            except FloodWaitError as e:
                logger.warning(f"Слишком много запросов, ждем {e.seconds} секунд")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Ошибка при пересылке сообщения {message.id}: {e}")
    
    async def stop_forward_monitoring(self, source_id, target_id):
        """Останавливает отслеживание новых сообщений"""
        source_id = str(source_id)
        # Проверяем, не является ли target_id сокращённой версией saved_messages
        if target_id == SAVED_MESSAGES_KEY:
            target_id = "saved_messages"
        else:
            target_id = str(target_id)
            
        logger.info(f"Попытка остановки пересылки: {source_id} -> {target_id}")
        
        # Проверяем наличие пересылки в словаре
        key = (source_id, target_id)
        
        if key in self.active_forwards:
            logger.info(f"Пересылка найдена в активных: {source_id} -> {target_id}")
            # Отмечаем, что пересылка остановлена
            self.active_forwards[key]['is_running'] = False
            
            # Удаляем обработчик события
            if key in self.handlers:
                try:
                    logger.info(f"Удаляем обработчик события для: {source_id} -> {target_id}")
                    self.client.remove_event_handler(self.handlers[key])
                    del self.handlers[key]
                    logger.info(f"Обработчик успешно удален для: {source_id} -> {target_id}")
                except Exception as e:
                    logger.error(f"Ошибка при удалении обработчика: {e}")
            
            # Удаляем из словаря активных пересылок
            del self.active_forwards[key]
            
            # Удаляем из списка пересланных групп, если есть
            if key in self.forwarded_groups:
                del self.forwarded_groups[key]
            
            # Удаляем из конфигурации
            self.config.remove_active_forward(source_id, target_id)
            logger.info(f"Пересылка успешно остановлена: {source_id} -> {target_id}")
            return True
        
        # Дополнительная проверка для saved_messages с другими вариантами записи
        if target_id == "saved_messages":
            # Попробуем поискать с альтернативным ключом
            alt_key = (source_id, SAVED_MESSAGES_KEY)
            if alt_key in self.active_forwards:
                logger.info(f"Пересылка найдена с альтернативным ключом: {source_id} -> {SAVED_MESSAGES_KEY}")
                # Отмечаем, что пересылка остановлена
                self.active_forwards[alt_key]['is_running'] = False
                
                # Удаляем обработчик события
                if alt_key in self.handlers:
                    try:
                        logger.info(f"Удаляем обработчик события для: {source_id} -> {SAVED_MESSAGES_KEY}")
                        self.client.remove_event_handler(self.handlers[alt_key])
                        del self.handlers[alt_key]
                        logger.info(f"Обработчик успешно удален для: {source_id} -> {SAVED_MESSAGES_KEY}")
                    except Exception as e:
                        logger.error(f"Ошибка при удалении обработчика: {e}")
                
                # Удаляем из словаря активных пересылок
                del self.active_forwards[alt_key]
                
                # Удаляем из списка пересланных групп, если есть
                if alt_key in self.forwarded_groups:
                    del self.forwarded_groups[alt_key]
                
                # Удаляем из конфигурации
                self.config.remove_active_forward(source_id, SAVED_MESSAGES_KEY)
                logger.info(f"Пересылка успешно остановлена с альтернативным ключом: {source_id} -> {SAVED_MESSAGES_KEY}")
                return True
        
        # Проверяем все активные пересылки для отладки
        for k in self.active_forwards.keys():
            logger.info(f"Активная пересылка: {k[0]} -> {k[1]}")
        
        logger.warning(f"Пересылка не найдена: {source_id} -> {target_id}")
        return False
    
    async def forward_all_media(self, progress_callback=None, limit=None):
        """Пересылка всех медиасообщений из источника в цель, начиная с самого старого"""
        if not self.source_chat or not self.target_chat:
            return False, 0
            
        count = 0
        # Словарь для отслеживания пересланных групп медиа
        forwarded_groups = set()
        
        try:
            # Определяем общее количество сообщений (для прогресса)
            total_messages = 0
            if progress_callback:
                logger.info("Начинаем подсчет сообщений...")
                messages_count_iter = self.client.iter_messages(self.source_chat)
                async for _ in messages_count_iter:
                    total_messages += 1
                    # Ограничиваем количество сообщений для подсчета
                    if total_messages >= 1000:
                        break
                
                logger.info(f"Найдено сообщений: {total_messages}")
                await progress_callback(0, total_messages, "Подсчет сообщений завершен")
            
            # Пересылаем сообщения в обратном порядке (от старых к новым)
            offset_id = 0
            batch_size = 100
            processed = 0
            
            while True:
                # Получаем пакет сообщений
                batch = []
                logger.info(f"Загружаем пакет сообщений с offset_id={offset_id}...")
                messages_iter = self.client.iter_messages(
                    self.source_chat, 
                    limit=batch_size, 
                    offset_id=offset_id,
                    reverse=True  # От старых к новым
                )
                
                async for message in messages_iter:
                    batch.append(message)
                    processed += 1
                    if limit and processed >= limit:
                        break
                
                logger.info(f"Загружено сообщений в пакете: {len(batch)}")
                
                if not batch:
                    logger.info("Больше сообщений не найдено.")
                    break
                
                # Обновляем offset_id для следующего пакета
                offset_id = batch[-1].id
                
                # Группируем сообщения по grouped_id
                messages_by_group = {}
                single_messages = []
                
                # Разделяем сообщения на группы и одиночные
                for message in batch:
                    if getattr(message, 'grouped_id', None):
                        # Если сообщение в группе, добавляем в соответствующий список
                        group_id = message.grouped_id
                        if group_id not in messages_by_group:
                            messages_by_group[group_id] = []
                        messages_by_group[group_id].append(message)
                    else:
                        # Одиночные сообщения обрабатываем отдельно
                        single_messages.append(message)
                
                # Обрабатываем одиночные сообщения
                for message in single_messages:
                    should_forward = self.should_forward_message(message)
                    if should_forward:
                        try:
                            await self.client.forward_messages(self.target_chat, message)
                            count += 1
                            delay = self.config.data['delay']
                            logger.info(f"Переслано сообщение #{count}. ID: {message.id}. Дата: {message.date}")
                            if progress_callback:
                                await progress_callback(count, total_messages, f"Переслано сообщений: {count}")
                            await asyncio.sleep(delay)
                        except FloodWaitError as e:
                            logger.warning(f"Слишком много запросов, ждем {e.seconds} секунд")
                            if progress_callback:
                                await progress_callback(count, total_messages, f"Слишком много запросов, ждем {e.seconds} секунд")
                            await asyncio.sleep(e.seconds)
                        except Exception as e:
                            logger.error(f"Ошибка при пересылке сообщения {message.id}: {e}")
                            continue
                
                # Обрабатываем группы сообщений
                for grouped_id, messages in messages_by_group.items():
                    # Пропускаем, если группа уже обработана
                    if grouped_id in forwarded_groups:
                        continue
                    
                    # Проверяем, есть ли в группе хотя бы одно сообщение, подходящее под критерии
                    has_matching_media = any(self.should_forward_message(m) for m in messages)
                    
                    if has_matching_media:
                        try:
                            # Пересылаем всю группу
                            await self.client.forward_messages(self.target_chat, messages)
                            count += len(messages)
                            forwarded_groups.add(grouped_id)  # Помечаем как переслано
                            delay = self.config.data['delay']
                            logger.info(f"Переслана группа медиа ({len(messages)} элементов). Всего: {count}")
                            if progress_callback:
                                await progress_callback(count, total_messages, f"Переслано сообщений: {count}")
                            await asyncio.sleep(delay)
                        except FloodWaitError as e:
                            logger.warning(f"Слишком много запросов, ждем {e.seconds} секунд")
                            if progress_callback:
                                await progress_callback(count, total_messages, f"Слишком много запросов, ждем {e.seconds} секунд")
                            await asyncio.sleep(e.seconds)
                        except Exception as e:
                            logger.error(f"Ошибка при пересылке группы медиа: {e}")
                            continue
                
                if limit and processed >= limit:
                    logger.info(f"Достигнут лимит сообщений: {limit}")
                    break
            
            # После пересылки всех сообщений запускаем мониторинг новых
            logger.info("Запускаем мониторинг новых сообщений...")
            await self.start_forward_monitoring(self.source_chat, self.target_chat, self.media_types)
            logger.info("Мониторинг новых сообщений запущен.")
                        
        except Exception as e:
            logger.error(f"Ошибка в процессе пересылки: {e}")
            return False, count
            
        return True, count
    
    async def get_active_forwards(self):
        """Получение списка активных пересылок"""
        result = []
        for (source_id, target_id), data in self.active_forwards.items():
            if data['is_running']:
                try:
                    source = await self.client.get_entity(int(source_id))
                    
                    if target_id == "saved_messages" or target_id == SAVED_MESSAGES_KEY:
                        target_name = "Избранное"
                    else:
                        target = await self.client.get_entity(int(target_id))
                        target_name = target.title if hasattr(target, 'title') else target.first_name
                    
                    result.append({
                        'source_id': source_id,
                        'source_name': source.title if hasattr(source, 'title') else source.first_name,
                        'target_id': target_id,
                        'target_name': target_name,
                        'media_types': list(data['media_types'])
                    })
                except Exception as e:
                    logger.error(f"Ошибка при получении данных активной пересылки: {e}")
        
        return result


# Функция для проверки доступа пользователя
def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id == ADMIN_USER_ID


async def main():
    """Основная функция запуска бота"""
    
    # Инициализация конфигурации
    config = Configuration()
    
    # Инициализация форвардера
    forwarder = MediaForwarder(API_ID, API_HASH, SESSION_NAME, config)
    await forwarder.connect()
    
    # Настройка бота для aiogram 3.x
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    
    # Обработчики сообщений и callback-запросов
    @dp.message(Command("start"))
    async def cmd_start(message: AiogramMessage):
        # Проверяем, является ли пользователь администратором
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📱 Создать пересылку")],
                [KeyboardButton(text="📋 Активные пересылки")],
                [KeyboardButton(text="⚙️ Настройки")]
            ],
            resize_keyboard=True
        )
        await message.answer(
            "Привет! Я помогу вам пересылать медиаконтент из одного чата в другой.\n\n"
            "• Нажмите «📱 Создать пересылку» для настройки новой пересылки\n"
            "• Нажмите «📋 Активные пересылки» для управления существующими\n"
            "• Нажмите «⚙️ Настройки» для настройки параметров пересылки\n\n"
            "Бот просмотрит все сообщения в выбранном источнике, начиная с самого первого, "
            "и если найдет выбранный тип медиаконтента, то перешлет его в указанный чат или в Избранное.",
            reply_markup=keyboard
        )
    
    @dp.message(F.text == "📱 Создать пересылку")
    async def create_forwarding(message: AiogramMessage, state: FSMContext):
        # Проверяем, является ли пользователь администратором
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        # Получаем список диалогов
        await show_dialog_list(message, state, is_source=True)
    
    @dp.message(F.text == "⚙️ Настройки")
    async def show_settings(message: AiogramMessage):
        # Проверяем, является ли пользователь администратором
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        current_delay = config.data['delay']
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Текущая задержка: {current_delay} сек", callback_data="settings_info")],
            [
                InlineKeyboardButton(text="1 сек", callback_data="delay_1"),
                InlineKeyboardButton(text="3 сек", callback_data="delay_3"),
                InlineKeyboardButton(text="5 сек", callback_data="delay_5")
            ],
            [
                InlineKeyboardButton(text="10 сек", callback_data="delay_10"),
                InlineKeyboardButton(text="30 сек", callback_data="delay_30")
            ]
        ])
        
        await message.answer(
            "⚙️ Настройки пересылки:\n\n"
            "Задержка между сообщениями влияет на скорость пересылки. "
            "Слишком маленькая задержка может привести к ограничениям со стороны Telegram.",
            reply_markup=keyboard
        )
    
    @dp.message(F.text == "📋 Активные пересылки")
    async def show_active_forwards(message: AiogramMessage):
        # Проверяем, является ли пользователь администратором
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        active_forwards = await forwarder.get_active_forwards()
        
        if not active_forwards:
            await message.answer("У вас нет активных пересылок. Нажмите «📱 Создать пересылку» для настройки.")
            return
            
        await create_forwarding_keyboard(message.chat.id, active_forwards)
    
    @dp.callback_query(lambda c: c.data.startswith('delay_'))
    async def process_delay_setting(callback_query: CallbackQuery):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        delay = int(callback_query.data.split('_')[1])
        config.set_delay(delay)
        await callback_query.answer(f"Задержка установлена на {delay} секунд")
        
        # Обновляем сообщение с настройками
        current_delay = config.data['delay']
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Текущая задержка: {current_delay} сек", callback_data="settings_info")],
            [
                InlineKeyboardButton(text="1 сек", callback_data="delay_1"),
                InlineKeyboardButton(text="3 сек", callback_data="delay_3"),
                InlineKeyboardButton(text="5 сек", callback_data="delay_5")
            ],
            [
                InlineKeyboardButton(text="10 сек", callback_data="delay_10"),
                InlineKeyboardButton(text="30 сек", callback_data="delay_30")
            ]
        ])
        
        await callback_query.message.edit_text(
            "⚙️ Настройки пересылки:\n\n"
            "Задержка между сообщениями влияет на скорость пересылки. "
            "Слишком маленькая задержка может привести к ограничениям со стороны Telegram.",
            reply_markup=keyboard
        )
    
    @dp.callback_query(lambda c: c.data.startswith('forward_stop_'))
    async def stop_forward(callback_query: CallbackQuery):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        parts = callback_query.data.split('_')
        source_id = parts[2]
        target_id = parts[3]
        
        logger.info(f"Получена команда остановки пересылки: {source_id} -> {target_id}")
        
        try:
            # Показываем промежуточное сообщение
            await callback_query.answer("Останавливаю пересылку...")
            status_message = await callback_query.message.edit_text(
                "⏳ Останавливаю пересылку...",
                reply_markup=None
            )
            
            # Запускаем остановку в отдельной задаче
            stop_task = asyncio.create_task(forwarder.stop_forward_monitoring(source_id, target_id))
            success = await stop_task
            
            if success:
                await status_message.edit_text("✅ Пересылка успешно остановлена!")
                # Обновляем список активных пересылок после небольшой задержки
                await asyncio.sleep(1)
                
                # Проверяем, есть ли активные пересылки
                active_forwards = await forwarder.get_active_forwards()
                if active_forwards:
                    # Отправляем новое сообщение вместо обновления старого
                    try:
                        # Используем метод отправки сообщения в чат
                        await bot.send_message(
                            chat_id=callback_query.message.chat.id,
                            text="Ваши активные пересылки:"
                        )
                        # После этого обновляем список активных пересылок
                        await create_forwarding_keyboard(callback_query.message.chat.id, active_forwards)
                    except Exception as e:
                        logger.error(f"Ошибка при обновлении списка активных пересылок: {e}")
                else:
                    # Если нет активных пересылок, сообщаем об этом
                    try:
                        await bot.send_message(
                            chat_id=callback_query.message.chat.id,
                            text="У вас нет активных пересылок. Нажмите «📱 Создать пересылку» для настройки."
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при отправке сообщения об отсутствии активных пересылок: {e}")
            else:
                await status_message.edit_text("❌ Не удалось остановить пересылку. Попробуйте еще раз.")
        except Exception as e:
            logger.error(f"Ошибка при остановке пересылки: {e}")
            try:
                # Используем метод edit_message_text вместо answer
                await bot.edit_message_text(
                    chat_id=callback_query.message.chat.id,
                    message_id=callback_query.message.message_id,
                    text=f"❌ Произошла ошибка при остановке пересылки"
                )
            except Exception as e2:
                logger.error(f"Не удалось отправить сообщение об ошибке: {e2}")
    
    async def create_forwarding_keyboard(chat_id, active_forwards):
        """Создаёт и отправляет клавиатуру с активными пересылками"""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
        
        for i, forward in enumerate(active_forwards):
            media_types_text = ", ".join([
                "Фото" if "photo" in forward['media_types'] else "",
                "Видео" if "video" in forward['media_types'] else "",
                "Документы" if "document" in forward['media_types'] else ""
            ]).replace(", ,", ",").strip(", ")
            
            # Используем сокращенный ключ для Избранного в callback_data
            target_id_for_callback = SAVED_MESSAGES_KEY if forward['target_id'] == "saved_messages" else forward['target_id']
            
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"{i+1}. {forward['source_name']} ➡️ {forward['target_name']}",
                    callback_data=f"forward_info_{forward['source_id']}_{target_id_for_callback}"
                )
            ])
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"📄 {media_types_text}",
                    callback_data=f"forward_types_{forward['source_id']}_{target_id_for_callback}"
                )
            ])
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text="🛑 Остановить",
                    callback_data=f"forward_stop_{forward['source_id']}_{target_id_for_callback}"
                )
            ])
        
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="Ваши активные пересылки:",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке клавиатуры с активными пересылками: {e}")
    
    @dp.callback_query(lambda c: c.data.startswith('dialog_source_'))
    async def process_source_selection(callback_query: CallbackQuery, state: FSMContext):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        chat_id = int(callback_query.data.split('_')[2])
        success, name = await forwarder.set_source(chat_id)
        
        if not success:
            await callback_query.answer("Ошибка при выборе источника. Попробуйте еще раз.")
            return
            
        await callback_query.answer(f"Выбран источник: {name}")
        
        # Переходим к выбору цели
        await show_target_options(callback_query.message)
    
    async def show_target_options(message):
        """Показывает варианты выбора цели (чат или Избранное)"""
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📁 Выбрать чат",
                    callback_data="target_select_chat"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Переслать в Избранное",
                    callback_data="target_saved_messages"
                )
            ]
        ])
        
        await message.answer("Выберите, куда пересылать контент:", reply_markup=keyboard)
    
    @dp.callback_query(lambda c: c.data == "target_select_chat")
    async def show_target_chat_selection(callback_query: CallbackQuery, state: FSMContext):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        await callback_query.answer()
        await show_dialog_list(callback_query.message, state, is_source=False)
    
    @dp.callback_query(lambda c: c.data == "target_saved_messages")
    async def select_saved_messages(callback_query: CallbackQuery):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        success, name = await forwarder.set_target(SAVED_MESSAGES_KEY)
        
        if not success:
            await callback_query.answer("Ошибка при выборе Избранного. Попробуйте еще раз.")
            return
            
        await callback_query.answer("Выбрано: Избранное")
        
        # Переходим к выбору типов медиа
        await show_media_selection(callback_query.message)
    
    @dp.callback_query(lambda c: c.data.startswith('dialog_target_'))
    async def process_target_selection(callback_query: CallbackQuery):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        chat_id = int(callback_query.data.split('_')[2])
        success, name = await forwarder.set_target(chat_id)
        
        if not success:
            await callback_query.answer("Ошибка при выборе цели. Попробуйте еще раз.")
            return
            
        await callback_query.answer(f"Выбрана цель: {name}")
        
        # Переходим к выбору типов медиа
        await show_media_selection(callback_query.message)
    
    async def show_dialog_list(message, state, is_source=True, offset=0, query=None):
        """Показывает список диалогов с пагинацией и поиском"""
        # Получаем диалоги с учетом пагинации и поиска
        dialogs, total = await forwarder.get_dialogs(offset=offset, limit=5, query=query)
        
        # Создаем клавиатуру
        keyboard = InlineKeyboardMarkup(inline_keyboard=[])
        
        # Добавляем кнопки для диалогов
        for dialog in dialogs:
            type_icon = "📢" if dialog['type'] == 'channel' else "👥" if dialog['type'] == 'group' else "👤"
            btn_text = f"{type_icon} {dialog['name']}"
            
            callback_data = f"dialog_source_{dialog['id']}" if is_source else f"dialog_target_{dialog['id']}"
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(text=btn_text, callback_data=callback_data)
            ])
        
        # Добавляем кнопки навигации
        nav_buttons = []
        
        if offset > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"nav_{'source' if is_source else 'target'}_{offset-5}_{query or ''}"
                )
            )
            
        if offset + 5 < total:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="▶️ Далее",
                    callback_data=f"nav_{'source' if is_source else 'target'}_{offset+5}_{query or ''}"
                )
            )
            
        if nav_buttons:
            keyboard.inline_keyboard.append(nav_buttons)
        
        # Добавляем кнопку поиска
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="🔍 Поиск",
                callback_data=f"search_{'source' if is_source else 'target'}"
            )
        ])
        
        text = "Выберите источник для пересылки:" if is_source else "Выберите чат для пересылки:"
        if query:
            text = f"Результаты поиска «{query}»:\n\n{text}"
            
        await message.answer(text, reply_markup=keyboard)
    
    @dp.callback_query(lambda c: c.data.startswith('nav_'))
    async def process_navigation(callback_query: CallbackQuery, state: FSMContext):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        parts = callback_query.data.split('_')
        is_source = parts[1] == 'source'
        offset = int(parts[2])
        query = parts[3] if len(parts) > 3 and parts[3] else None
        
        await callback_query.message.delete()
        await show_dialog_list(callback_query.message, state, is_source, offset, query)
    
    @dp.callback_query(lambda c: c.data.startswith('search_'))
    async def process_search_request(callback_query: CallbackQuery, state: FSMContext):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        is_source = callback_query.data.split('_')[1] == 'source'
        
        # Сохраняем информацию о том, для чего выполняется поиск
        await state.update_data(search_for=is_source)
        
        # Устанавливаем состояние ожидания поискового запроса
        await state.set_state(ForwardingStates.waiting_for_search)
        
        await callback_query.message.answer("Введите поисковый запрос:")
    
    @dp.message(ForwardingStates.waiting_for_search)
    async def process_search_query(message: AiogramMessage, state: FSMContext):
        # Проверяем, является ли пользователь администратором
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        # Получаем данные из состояния
        data = await state.get_data()
        is_source = data.get('search_for', True)
        
        # Сбрасываем состояние
        await state.clear()
        
        # Показываем результаты поиска
        await show_dialog_list(message, state, is_source, offset=0, query=message.text)
    
    async def show_media_selection(message):
        """Показывает экран выбора типов медиаконтента"""
        # Создаем клавиатуру для выбора типов медиа
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📷 Фото",
                    callback_data="media_photo"
                ),
                InlineKeyboardButton(
                    text="🎥 Видео",
                    callback_data="media_video"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📎 Документы",
                    callback_data="media_document"
                ),
                InlineKeyboardButton(
                    text="📄 Все типы",
                    callback_data="media_all"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Продолжить",
                    callback_data="continue_setup"
                )
            ]
        ])
        
        source_name = forwarder.source_chat.title if hasattr(forwarder.source_chat, 'title') else forwarder.source_chat.first_name
        target_name = "Избранное" if forwarder.target_chat.id == forwarder.saved_messages.id else \
                      forwarder.target_chat.title if hasattr(forwarder.target_chat, 'title') else forwarder.target_chat.first_name
        
        await message.answer(
            f"Настройка пересылки:\n\n"
            f"📤 Источник: {source_name}\n"
            f"📥 Цель: {target_name}\n\n"
            f"Выберите типы медиаконтента для пересылки:",
            reply_markup=keyboard
        )
    
    @dp.callback_query(lambda c: c.data.startswith('media_'))
    async def process_media_selection(callback_query: CallbackQuery):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        media_type = callback_query.data.split('_')[1]
        
        if media_type == 'all':
            forwarder.media_types = {'photo', 'video', 'document'}
            await callback_query.answer("Выбраны все типы медиа")
        else:
            if media_type in forwarder.media_types:
                forwarder.media_types.remove(media_type)
                status = "удален из списка"
            else:
                forwarder.media_types.add(media_type)
                status = "добавлен в список"
                
            await callback_query.answer(f"{media_type.capitalize()} {status}")
        
        # Обновляем клавиатуру, чтобы показать выбранные типы
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📷 Фото {'✅' if 'photo' in forwarder.media_types else ''}",
                    callback_data="media_photo"
                ),
                InlineKeyboardButton(
                    text=f"🎥 Видео {'✅' if 'video' in forwarder.media_types else ''}",
                    callback_data="media_video"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📎 Документы {'✅' if 'document' in forwarder.media_types else ''}",
                    callback_data="media_document"
                ),
                InlineKeyboardButton(
                    text="📄 Все типы",
                    callback_data="media_all"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Продолжить",
                    callback_data="continue_setup"
                )
            ]
        ])
        
        source_name = forwarder.source_chat.title if hasattr(forwarder.source_chat, 'title') else forwarder.source_chat.first_name
        target_name = "Избранное" if forwarder.target_chat.id == forwarder.saved_messages.id else \
                      forwarder.target_chat.title if hasattr(forwarder.target_chat, 'title') else forwarder.target_chat.first_name
        
        await callback_query.message.edit_text(
            f"Настройка пересылки:\n\n"
            f"📤 Источник: {source_name}\n"
            f"📥 Цель: {target_name}\n\n"
            f"Выберите типы медиаконтента для пересылки:",
            reply_markup=keyboard
        )
    
    @dp.callback_query(lambda c: c.data == 'continue_setup')
    async def show_limit_options(callback_query: CallbackQuery, state: FSMContext):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Все сообщения",
                    callback_data="limit_all"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Последние 100",
                    callback_data="limit_100"
                ),
                InlineKeyboardButton(
                    text="Последние 500",
                    callback_data="limit_500"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Последние 1000",
                    callback_data="limit_1000"
                ),
                InlineKeyboardButton(
                    text="Указать вручную",
                    callback_data="limit_custom"
                )
            ]
        ])
        
        await callback_query.message.edit_text(
            "Сколько сообщений обработать?\n\n"
            "⚠️ Внимание: обработка всех сообщений может занять много времени, "
            "особенно для больших каналов и групп.",
            reply_markup=keyboard
        )
    
    @dp.callback_query(lambda c: c.data.startswith('limit_'))
    async def process_limit_selection(callback_query: CallbackQuery, state: FSMContext):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        limit_option = callback_query.data.split('_')[1]
        
        if limit_option == 'custom':
            await state.set_state(ForwardingStates.waiting_for_limit)
            await callback_query.message.edit_text(
                "Введите максимальное количество сообщений для обработки (число):"
            )
            return
        
        # Определяем лимит
        limit = None if limit_option == 'all' else int(limit_option)
        
        # Запускаем процесс пересылки
        await start_forwarding_process(callback_query.message, limit)
    
    @dp.message(ForwardingStates.waiting_for_limit)
    async def process_custom_limit(message: AiogramMessage, state: FSMContext):
        # Проверяем, является ли пользователь администратором
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        try:
            limit = int(message.text.strip())
            if limit <= 0:
                await message.answer("Пожалуйста, введите положительное число.")
                return
                
            await state.clear()
            await start_forwarding_process(message, limit)
        except ValueError:
            await message.answer("Пожалуйста, введите корректное число.")
    
    async def update_progress_message(message_id, chat_id, count, total, status_text):
        """Обновляет сообщение о прогрессе пересылки"""
        try:
            percentage = min(100, int(count / total * 100)) if total > 0 else 0
            progress_bar = "".join(["█" if i <= percentage // 5 else "░" for i in range(20)])
            
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"⏳ Прогресс пересылки: {percentage}% [{count}/{total if total else '?'}]\n"
                     f"[{progress_bar}]\n\n"
                     f"{status_text}"
            )
        except Exception as e:
            logger.error(f"Ошибка при обновлении прогресса: {e}")
    
    async def start_forwarding_process(message, limit=None):
        """Запускает процесс пересылки сообщений"""
        # Создаем сообщение о прогрессе
        progress_message = await message.answer("⏳ Подготовка к пересылке...")
        
        # Создаем callback для обновления прогресса
        async def progress_callback(count, total, status_text):
            await update_progress_message(
                progress_message.message_id,
                progress_message.chat.id,
                count,
                total,
                status_text
            )
        
        # Запускаем пересылку в отдельной задаче
        task = asyncio.create_task(
            forwarder.forward_all_media(
                progress_callback=progress_callback,
                limit=limit
            )
        )
        
        # Ожидаем завершения задачи
        try:
            success, count = await task
            
            source_name = forwarder.source_chat.title if hasattr(forwarder.source_chat, 'title') else forwarder.source_chat.first_name
            target_name = "Избранное" if forwarder.target_chat.id == forwarder.saved_messages.id else \
                          forwarder.target_chat.title if hasattr(forwarder.target_chat, 'title') else forwarder.target_chat.first_name
            
            source_id = forwarder.source_chat.id
            # Используем сокращенный ключ для Избранного в callback_data
            target_id = SAVED_MESSAGES_KEY if forwarder.target_chat.id == forwarder.saved_messages.id else forwarder.target_chat.id
            
            if success:
                # Клавиатура для остановки пересылки
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🛑 Остановить пересылку новых сообщений",
                            callback_data=f"forward_stop_{source_id}_{target_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📋 Активные пересылки",
                            callback_data="show_active_forwards"
                        )
                    ]
                ])
                
                await progress_message.edit_text(
                    f"✅ Пересылка завершена!\n\n"
                    f"📤 Источник: {source_name}\n"
                    f"📥 Цель: {target_name}\n"
                    f"📊 Переслано сообщений: {count}\n\n"
                    f"Новые сообщения будут автоматически пересылаться.",
                    reply_markup=keyboard
                )
            else:
                await progress_message.edit_text(
                    f"⚠️ Возникла ошибка при пересылке.\n"
                    f"Переслано сообщений: {count}\n\n"
                    f"Попробуйте еще раз или проверьте права доступа."
                )
        except Exception as e:
            logger.error(f"Ошибка при пересылке: {e}")
            await progress_message.edit_text(
                f"❌ Произошла ошибка: {str(e)}\n"
                f"Попробуйте еще раз или выберите другие параметры."
            )
    
    @dp.callback_query(lambda c: c.data == 'show_active_forwards')
    async def callback_show_active_forwards(callback_query: CallbackQuery):
        # Проверяем, является ли пользователь администратором
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
            return
        
        await callback_query.answer()
        active_forwards = await forwarder.get_active_forwards()
        if not active_forwards:
            await callback_query.message.answer("У вас нет активных пересылок. Нажмите «📱 Создать пересылку» для настройки.")
        else:
            await create_forwarding_keyboard(callback_query.message.chat.id, active_forwards)
    
    # Обработчик для всех остальных сообщений от неадминистраторов
    @dp.message()
    async def handle_all_messages(message: AiogramMessage):
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
    
    # Обработчик для всех остальных callback-запросов от неадминистраторов
    @dp.callback_query()
    async def handle_all_callbacks(callback_query: CallbackQuery):
        if not is_admin(callback_query.from_user.id):
            await callback_query.answer("⛔ Доступ запрещен. Этот бот доступен только для администратора.")
    
    # Запуск бота для aiogram 3.x
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
