import os
import sys
from dotenv import load_dotenv

# Add the project root to sys.path so we can import local modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from reasoning.flash_client import generate_flash_response

def main():
    print("=== Testing Gemini Flash Client ===")
    load_dotenv()
    
    if not os.getenv("GEMINI_API_KEY"):
        print("Error: GEMINI_API_KEY is not set in your .env file.")
        return
        
    dummy_context = "<CONTEXT>\n<GOALS>\nTest goals.\n</GOALS>\n</CONTEXT>"
    dummy_history = [
        {"role": "user", "content": "What are my priorities?"},
        {"role": "assistant", "content": "Your priority is to complete the MVP for David."}
    ]
    test_message = "Hi David, can you schedule 2 hours of deep work tomorrow morning?"
    
    print(f"\nSending message: '{test_message}'")
    print("With simulated chat history...")
    print("Waiting for response...")
    
    try:
        response = generate_flash_response(user_message=test_message, context_block=dummy_context, chat_history=dummy_history)
        print("\n--- Response Received ---")
        print(f"Message: {response.message}")
        print(f"Should Escalate: {response.should_escalate}")
        print(f"Escalation Reason: {response.escalation_reason}")
        print("-------------------------")
        
        if response.should_escalate:
            print("\nSuccess! The model correctly identified a scheduling request and triggered the escalation flag.")
        else:
            print("\nWarning: The model failed to trigger the escalation flag for a scheduling request.")
            
    except Exception as e:
        print(f"\nAn error occurred: {e}")

if __name__ == "__main__":
    main()
