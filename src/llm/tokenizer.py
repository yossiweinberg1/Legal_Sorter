from tokenizers import Tokenizer, models, trainers, pre_tokenizers

def build_tokenizer(texts, save_path="src/llm/tokenizer.json"):
    tokenizer = Tokenizer(models.BPE())
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()

    trainer = trainers.BpeTrainer(vocab_size=20000, min_frequency=2)
    tokenizer.train_from_iterator(texts, trainer)

    tokenizer.save(save_path)
    return tokenizer
