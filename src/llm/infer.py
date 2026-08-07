import torch
import os
from model import TinyTransformerLM
from tokenizers import Tokenizer

def generate(prompt, model_path="src/llm/model.pt", tokenizer_path="src/llm/tokenizer.json", max_tokens=200, log_callback=print, stop_check=lambda: False):
    # 🔥 Fix crash: Gracefully check if the file exists before attempting to load it
    if not os.path.exists(model_path):
        log_callback("❌ Generation Error: 'model.pt' weight profile not found. Please click 'Train LLM on DB' first.")
        return "Error: No trained model weights found. Please train the model before running analysis."

    tokenizer = Tokenizer.from_file(tokenizer_path)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    model = TinyTransformerLM(vocab_size=tokenizer.get_vocab_size())
    model.load_state_dict(torch.load(model_path, map_location=device))
    model.to(device)
    model.eval()

    ids = tokenizer.encode(prompt).ids
    x = torch.tensor(ids, dtype=torch.long, device=device).unsqueeze(0)

    log_callback("🔮 Generating analytical case response tokens...")
    
    with torch.no_grad():
        for i in range(max_tokens):
            if stop_check():
                log_callback("🛑 Generation process halted by user request.")
                break
                
            x_input = x[:, -1024:] 
            logits = model(x_input)
            next_id = torch.argmax(logits[:, -1, :], dim=-1, keepdim=True)
            x = torch.cat([x, next_id], dim=1)
            
            # Print a tick directly to the console window every 25 tokens generated
            if i % 25 == 0 and i > 0:
                log_callback(f"Tokens streamed: {i}...")

    return tokenizer.decode(x[0].tolist())