import torch
from model import TinyTransformerLM
from tokenizers import Tokenizer

def generate(prompt, model_path, tokenizer_path, max_tokens=200):
    tokenizer = Tokenizer.from_file(tokenizer_path)
    model = TinyTransformerLM(vocab_size=tokenizer.get_vocab_size())
    model.load_state_dict(torch.load(model_path))
    model.eval()

    ids = tokenizer.encode(prompt).ids
    x = torch.tensor(ids).unsqueeze(0)

    for _ in range(max_tokens):
        logits = model(x)
        next_id = torch.argmax(logits[:, -1, :], dim=-1)
        x = torch.cat([x, next_id.unsqueeze(0)], dim=1)

    return tokenizer.decode(x[0].tolist())
