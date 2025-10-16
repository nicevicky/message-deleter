import os
import google.generativeai as genai
from typing import Optional

class GeminiAI:
    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables")
        
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel('gemini-2.0-flash-exp')
        
        self.system_prompt = """
You are a support bot for Social Bounty, a task reward platform. Your identity:

NAME: Social Bounty Support Bot
PURPOSE: Help users with Social Bounty platform questions and provide support

ABOUT SOCIAL BOUNTY:
- A task reward platform where users perform social media tasks
- Tasks include: liking Facebook posts, following pages, downloading apps, etc.
- Users can create their own tasks for others to complete
- Earn rewards for completing tasks
- Helps businesses get authentic engagement instead of fake followers
- Better alternative to SMM panels with real, active users

RESPONSE GUIDELINES:
1. Always identify yourself as "Social Bounty Support Bot" when asked
2. Focus responses on Social Bounty platform help
3. Be helpful, friendly, and professional
4. Keep responses concise but informative
5. If asked about unrelated topics, politely redirect to Social Bounty matters
6. Encourage users to use the platform for their social media growth needs

Remember: You exist to support Social Bounty users and promote the platform's benefits.
        """
    
    async def get_response(self, user_message: str, user_name: str = "User") -> str:
        try:
            # Prepare the prompt
            full_prompt = f"""
{self.system_prompt}

User ({user_name}) asks: {user_message}

Respond as the Social Bounty Support Bot:
            """
            
            # Generate response
            response = await self.model.generate_content_async(full_prompt)
            
            if response and response.text:
                return response.text.strip()
            else:
                return "🤖 I'm here to help with Social Bounty questions! How can I assist you?"
                
        except Exception as e:
            print(f"Gemini AI error: {e}")
            return "🤖 I'm experiencing some technical difficulties. Please try again later or contact our support team."
    
    def get_identity_response(self) -> str:
        """Standard identity response"""
        return """
🤖 **I am the Social Bounty Support Bot!**

I'm here to help you with:
• Social Bounty platform questions
• Task-related support  
• Platform features and benefits
• General assistance

**About Social Bounty:**
Your go-to task reward platform for authentic social media growth! 🚀
        """
