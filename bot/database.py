import os
import asyncio
from typing import List, Dict, Optional
import json
from datetime import datetime

class Database:
    def __init__(self):
        # For simplicity, using file-based storage
        # In production, use PostgreSQL or similar
        self.data_file = "bot_data.json"
        self.data = self.load_data()
    
    def load_data(self) -> dict:
        """Load data from file"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error loading data: {e}")
        
        return {
            "users": {},
            "interactions": [],
            "stats": {}
        }
    
    def save_data(self):
        """Save data to file"""
        try:
            with open(self.data_file, 'w') as f:
                json.dump(self.data, f, indent=2, default=str)
        except Exception as e:
            print(f"Error saving data: {e}")
    
    async def add_user(self, user_id: int, first_name: str, username: str = None):
        """Add or update user"""
        user_key = str(user_id)
        
        if user_key not in self.data["users"]:
            self.data["users"][user_key] = {
                "id": user_id,
                "first_name": first_name,
                "username": username,
                "join_date": datetime.now().isoformat(),
                "message_count": 0,
                "ai_interactions": 0
            }
        else:
            # Update existing user
            self.data["users"][user_key]["first_name"] = first_name
            self.data["users"][user_key]["username"] = username
        
        self.save_data()
    
    async def log_interaction(self, user_id: int, question: str, response: str):
        """Log AI interaction"""
        user_key = str(user_id)
        
        # Update user stats
        if user_key in self.data["users"]:
            self.data["users"][user_key]["ai_interactions"] += 1
        
        # Log interaction
        self.data["interactions"].append({
            "user_id": user_id,
            "question": question,
            "response": response,
            "timestamp": datetime.now().isoformat()
        })
        
        # Keep only last 1000 interactions
        if len(self.data["interactions"]) > 1000:
            self.data["interactions"] = self.data["interactions"][-1000:]
        
        self.save_data()
    
    async def increment_message_count(self, user_id: int):
        """Increment user message count"""
        user_key = str(user_id)
        if user_key in self.data["users"]:
            self.data["users"][user_key]["message_count"] += 1
            self.save_data()
    
    async def get_top_users(self, limit: int = 10) -> List[Dict]:
        """Get top active users"""
        users = list(self.data["users"].values())
        
        # Sort by activity score (messages + AI interactions)
        users.sort(
            key=lambda x: x.get("message_count", 0) + x.get("ai_interactions", 0) * 2,
            reverse=True
        )
        
        return users[:limit]
    
    async def get_user_stats(self, user_id: int) -> Optional[Dict]:
        """Get user statistics"""
        user_key = str(user_id)
        return self.data["users"].get(user_key)
