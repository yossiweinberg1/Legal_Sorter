import torch
from torch.utils.data import Dataset, DataLoader
from tokenizer import build_tokenizer
from model import TinyTransformerLM

class TextDataset(Dataset):
    def __init__(self, token_ids, seq_len=512):
        self.data = token_ids
        self.seq_len = seq_len

    def __len__(self):
        return len(self.data) - self.seq_len

    def __getitem__(self, idx):
        x = self.data[idx:idx+self.seq_len]
        y = self.data[idx+1:idx+self.seq_len+1]
        return torch.tensor(x), torch.tensor(y)

def train_llm(texts):
    tokenizer = build_tokenizer(texts)
    token_ids = tokenizer.encode_batch(texts)
    flat = [id for seq in token_ids for id in seq.ids]

    dataset = TextDataset(flat)
    loader = DataLoader(dataset, batch_size=4, shuffle=True)

    model = TinyTransformerLM(vocab_size=tokenizer.get_vocab_size())
    optim = torch.optim.Adam(model.parameters(), lr=3e-4)
    loss_fn = torch.nn.CrossEntropyLoss()

    for epoch in range(3):
        for x, y in loader:
            logits = model(x)
            loss = loss_fn(logits.view(-1, logits.size(-1)), y.view(-1))
            optim.zero_grad()
            loss.backward()
            optim.step()
            print("loss:", float(loss))
