import json
import os
from datetime import datetime, timedelta
from typing import Dict, List
from PIL import Image, ImageDraw, ImageFont
import io
import base64

class UserStats:
    def __init__(self):
        self.stats_file = "user_stats.json"
        self.load_stats()
    
    def load_stats(self):
        try:
            if os.path.exists(self.stats_file):
                with open(self.stats_file, 'r') as f:
                    self.stats = json.load(f)
            else:
                self.stats = {}
        except:
            self.stats = {}
    
    def save_stats(self):
        try:
            with open(self.stats_file, 'w') as f:
                json.dump(self.stats, f, indent=2)
        except:
            pass
    
    def update_user_activity(self, user_id: str, username: str, activity_type: str):
        if user_id not in self.stats:
            self.stats[user_id] = {
                "username": username,
                "messages": 0,
                "questions": 0,
                "helpful_responses": 0,
                "last_active": None
            }
        
        self.stats[user_id]["username"] = username
        self.stats[user_id]["last_active"] = datetime.now().isoformat()
        
        if activity_type == "message":
            self.stats[user_id]["messages"] += 1
        elif activity_type == "question":
            self.stats[user_id]["questions"] += 1
        elif activity_type == "helpful":
            self.stats[user_id]["helpful_responses"] += 1
        
        self.save_stats()
    
    def get_top_users(self, limit: int = 5) -> List[Dict]:
        users = []
        for user_id, data in self.stats.items():
            score = (data["messages"] * 1 + 
                    data["questions"] * 2 + 
                    data["helpful_responses"] * 3)
            users.append({
                "user_id": user_id,
                "username": data["username"],
                "score": score,
                "messages": data["messages"],
                "questions": data["questions"],
                "helpful_responses": data["helpful_responses"]
            })
        
        return sorted(users, key=lambda x: x["score"], reverse=True)[:limit]

def create_top_users_image(top_users: List[Dict]) -> io.BytesIO:
    # Create image
    width, height = 800, 600
    img = Image.new('RGB', (width, height), color='#2C3E50')
    draw = ImageDraw.Draw(img)
    
    try:
        title_font = ImageFont.truetype("arial.ttf", 36)
        user_font = ImageFont.truetype("arial.ttf", 24)
        stat_font = ImageFont.truetype("arial.ttf", 18)
    except:
        title_font = ImageFont.load_default()
        user_font = ImageFont.load_default()
        stat_font = ImageFont.load_default()
    
    # Title
    draw.text((width//2 - 150, 30), "🏆 Top Users - Social Bounty", 
              fill='#F39C12', font=title_font)
    
    y_offset = 100
    for i, user in enumerate(top_users, 1):
        # Rank and username
        rank_text = f"#{i} @{user['username']}"
        draw.text((50, y_offset), rank_text, fill='#ECF0F1', font=user_font)
        
        # Stats
        stats_text = f"Messages: {user['messages']} | Questions: {user['questions']} | Score: {user['score']}"
        draw.text((70, y_offset + 30), stats_text, fill='#BDC3C7', font=stat_font)
        
        # Separator line
        draw.line([(50, y_offset + 60), (width - 50, y_offset + 60)], fill='#34495E', width=2)
        
        y_offset += 80
    
    # Footer
    draw.text((width//2 - 100, height - 40), "Social Bounty Group Stats", 
              fill='#95A5A6', font=stat_font)
    
    # Save to bytes
    img_bytes = io.BytesIO()
    img.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    
    return img_bytes

def should_delete_message(text: str) -> bool:
    """Check if message contains filtered words"""
    if not text:
        return False
    
    text_lower = text.lower()
    from config import FILTERED_WORDS
    
    return any(word.lower() in text_lower for word in FILTERED_WORDS)

def is_question_about_social_bounty(text: str) -> bool:
    """Check if the message is a question about Social Bounty"""
    if not text:
        return False
    
    question_indicators = ['?', 'how', 'what', 'why', 'when', 'where', 'help', 'support']
    social_bounty_keywords = ['social bounty', 'task', 'reward', 'platform', 'facebook', 'follow', 'like']
    
    text_lower = text.lower()
    
    has_question = any(indicator in text_lower for indicator in question_indicators)
    has_sb_keyword = any(keyword in text_lower for keyword in social_bounty_keywords)
    
    return has_question or has_sb_keyword or len(text.split()) > 3
