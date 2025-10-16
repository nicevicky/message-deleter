from typing import List, Dict, Optional
from telegram import User

def is_admin(user_id: int, admin_ids: List[int]) -> bool:
    """Check if user is admin"""
    return user_id in admin_ids

async def get_user_stats(user_id: int, db) -> Optional[Dict]:
    """Get user statistics"""
    return await db.get_user_stats(user_id)

def format_user_mention(user: User) -> str:
    """Format user mention"""
    if user.username:
        return f"@{user.username}"
    else:
        return f"[{user.first_name}](tg://user?id={user.id})"

def truncate_text(text: str, max_length: int = 100) -> str:
    """Truncate text to max length"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."
