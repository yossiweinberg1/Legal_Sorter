import torch
import torch.nn as nn


class TinyTransformerLM(nn.Module):
    """Causal transformer language model with optional citation-graph conditioning.

    When ``graph_vec_size > 0`` (default: 64), a learned linear projection maps
    the per-token graph coordinate vector into the model's residual stream at the
    very first layer.  Pass ``graph_vecs=None`` to skip conditioning entirely
    (e.g. during pure inference when no graph context is available).
    """

    def __init__(
        self,
        vocab_size: int,
        d_model: int = 512,
        n_heads: int = 8,
        n_layers: int = 8,
        max_len: int = 1024,
        graph_vec_size: int = 64,
    ):
        super().__init__()
        self.embed = nn.Embedding(vocab_size, d_model)
        self.pos = nn.Embedding(max_len, d_model)

        if graph_vec_size > 0:
            # Projects the 64-dim citation graph vector into the residual stream
            self.graph_proj = nn.Linear(graph_vec_size, d_model)
        else:
            self.graph_proj = None

        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_heads, dim_feedforward=2048, batch_first=True
        )
        self.transformer = nn.TransformerEncoder(layer, num_layers=n_layers)
        self.fc = nn.Linear(d_model, vocab_size)

    def forward(self, x: torch.Tensor, graph_vecs: torch.Tensor | None = None) -> torch.Tensor:
        """
        Args:
            x: Token ids, shape (B, T).
            graph_vecs: Citation graph coordinates, shape (B, T, graph_vec_size).
                        Pass None to skip graph conditioning (pure LM mode).
        Returns:
            Logits, shape (B, T, vocab_size).
        """
        b, t = x.size()
        pos = torch.arange(t, device=x.device).unsqueeze(0).expand(b, t)
        h = self.embed(x) + self.pos(pos)

        # Inject graph context into the residual stream when available
        if self.graph_proj is not None and graph_vecs is not None:
            h = h + self.graph_proj(graph_vecs)

        mask = nn.Transformer.generate_square_subsequent_mask(t, device=x.device)
        h = self.transformer(h, mask=mask, is_causal=True)

        return self.fc(h)
