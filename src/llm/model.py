import torch
import torch.nn as nn

class TinyTransformerLM(nn.Module):
    def __init__(self, vocab_size, d_model=512, n_heads=8, n_layers=8, max_len=1024):
        super().__init__()
        self.embed = nn.Embedding(vocab_size, d_model)
        self.pos = nn.Embedding(max_len, d_model)

        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_heads, dim_feedforward=2048
        )
        self.transformer = nn.TransformerEncoder(layer, num_layers=n_layers)
        self.fc = nn.Linear(d_model, vocab_size)

    def forward(self, x):
        b, t = x.size()
        pos = torch.arange(t, device=x.device).unsqueeze(0).expand(b, t)
        h = self.embed(x) + self.pos(pos)
        h = self.transformer(h)
        return self.fc(h)
