import torch
import os
from model import TinyTransformerLM
from tokenizers import Tokenizer


def _sample_next_token(logits: torch.Tensor, temperature: float, top_k: int) -> torch.Tensor:
    """Apply temperature scaling and top-k filtering, then sample."""
    logits = logits / max(temperature, 1e-6)
    if top_k > 0:
        # Zero out all logits below the top-k threshold
        values, _ = torch.topk(logits, min(top_k, logits.size(-1)))
        logits[logits < values[:, -1:]] = float("-inf")
    probs = torch.softmax(logits, dim=-1)
    return torch.multinomial(probs, num_samples=1)


def generate(
    prompt,
    model_path="src/llm/model.pt",
    tokenizer_path="src/llm/tokenizer.json",
    max_tokens=200,
    temperature=0.8,
    top_k=50,
    log_callback=print,
    stop_check=lambda: False,
):
    """Generate text from the trained local model.

    Args:
        prompt: Input text to continue from.
        model_path: Path to trained model weights.
        tokenizer_path: Path to saved tokenizer JSON.
        max_tokens: Maximum new tokens to generate.
        temperature: Sampling temperature (0 = greedy, 1 = unscaled, >1 = more random).
        top_k: Keep only the top-k most likely tokens at each step (0 = disabled).
        log_callback: Function that receives progress messages.
        stop_check: Zero-argument callable that returns True when generation should halt.
    """
    if not os.path.exists(model_path):
        log_callback(
            "❌ Generation Error: 'model.pt' not found. "
            "Please click 'Train LLM on DB' first."
        )
        return "Error: No trained model weights found. Train the model before running analysis."

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

            next_logits = logits[:, -1, :]  # (1, vocab_size)

            if temperature <= 0 or top_k == 1:
                # Pure greedy decode
                next_id = torch.argmax(next_logits, dim=-1, keepdim=True)
            else:
                next_id = _sample_next_token(next_logits, temperature, top_k)

            x = torch.cat([x, next_id], dim=1)

            if i % 25 == 0 and i > 0:
                log_callback(f"Tokens streamed: {i}...")

    return tokenizer.decode(x[0].tolist())
