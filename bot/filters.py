import os
import json
from typing import Set, List

class WordFilter:
    def __init__(self):
        self.filtered_words: Set[str] = set()
        self.load_default_filters()
    
    def load_default_filters(self):
        """Load default filtered words"""
        default_words = [
            "bandwidth", "bandwith", "band width",
            "spam", "scam", "fake", "bot",
            "join my channel", "free money",
            "click here", "bit.ly", "tinyurl"
        ]
        self.filtered_words.update(default_words)
    
    def add_word(self, word: str):
        """Add word to filter"""
        self.filtered_words.add(word.lower().strip())
    
    def remove_word(self, word: str):
        """Remove word from filter"""
        self.filtered_words.discard(word.lower().strip())
    
    def contains_filtered_words(self, text: str) -> bool:
        """Check if text contains filtered words"""
        if not text:
            return False
        
        text_lower = text.lower()
        return any(word in text_lower for word in self.filtered_words)
    
    def get_filtered_words(self) -> List[str]:
        """Get list of filtered words"""
        return sorted(list(self.filtered_words))
