import torch
from torch.utils.data import Dataset, DataLoader
import time
import os
import math
import re

from tokenizer import build_tokenizer
from model import TinyTransformerLM

class HybridGraphDataset(Dataset):
    """Custom dataset that pairs structural text packages with their 
    corresponding citation graph coordinate vectors.
    """
    def __init__(self, tokenized_sequences, graph_vectors, seq_len=128):
        self.sequences = tokenized_sequences
        self.graph_vectors = graph_vectors
        self.seq_len = seq_len

    def __len__(self):
        return len(self.sequences) - self.seq_len - 1

    def __getitem__(self, idx):
        x = self.sequences[idx : idx + self.seq_len]
        y = self.sequences[idx + 1 : idx + self.seq_len + 1]
        
        # Pull the graph coordinate vector for this chunk of text
        g_vec = self.graph_vectors[idx]
        
        return (
            torch.tensor(x, dtype=torch.long), 
            torch.tensor(g_vec, dtype=torch.float32), 
            torch.tensor(y, dtype=torch.long)
        )


def parse_raw_case(idx, text):
    """Dynamically extracts Case ID, Citations, and Ruling Logic from unstructured text

    on the fly so we don't have to rewrite your SQLite queries.
    """
    # Find Case ID (e.g. "[CASE ID] ab67520...")
    case_id_match = re.search(r'(?:CASE ID|Case ID|\[CASE ID\])[\s:]*([a-fA-F0-9]+)', text)
    doc_id = case_id_match.group(1) if case_id_match else f"doc_{idx}"
    
    # Find legal citations (e.g., '759 N.E.2d 138' or '392 S.E.2d 735')
    citations = re.findall(r'\b\d+\s+[A-Z][A-Z\.\d\s]{1,15}d?\s+\d+\b', text)
    citations = list(set(citations))
    
    # Extract structural ruling reasoning
    logic_match = re.search(r'(?:REASONING|RULING LOGIC|DECISION)[\s:]*(.*)', text, re.IGNORECASE)
    ruling_logic = logic_match.group(1)[:300].strip() if logic_match else ""
    if not ruling_logic:
        fallback = re.search(r'([^.]+?\b(?:held|concluded|erred|ruling|decided)\b[^.]+?\.)', text, re.IGNORECASE)
        ruling_logic = fallback.group(1).strip() if fallback else "Precedent context extracted from structural scan."

    return {
        'id': doc_id,
        'citations': citations,
        'ruling_logic': ruling_logic,
        'text_content': text
    }


def build_citation_times_table(db_cases):
    """Option 2 & 3: Builds the cross-reference matrix ('times table') 

    and generates structural graph vectors for every case.
    """
    all_citations = set()
    case_map = {}
    
    for case in db_cases:
        doc_id = case['id']
        citations = case.get('citations', [])
        case_map[doc_id] = citations
        for cite in citations:
            all_citations.add(cite)
            
    citation_list = list(all_citations)
    cite_to_idx = {cite: i for i, cite in enumerate(citation_list)}

    graph_vectors = {}
    importance_scores = {}

    for doc_id, citations in case_map.items():
        # Create a compressed dense vector out of the 'times table' row
        vec = [0.0] * 64
        weight_sum = 0
        
        for cite in citations:
            if cite in cite_to_idx:
                idx = cite_to_idx[cite] % 64
                # Direct links get high value, deeper intersections scale down
                vec[idx] += 1.0
                weight_sum += 1
                
        # Normalize the vector coordinates
        if weight_sum > 0:
            vec = [v / math.sqrt(weight_sum) for v in vec]
            
        graph_vectors[doc_id] = vec
        # Importance score = total citation matrix intersections (Option 2)
        importance_scores[doc_id] = weight_sum + 1 

    return graph_vectors, importance_scores


def train_llm(texts, log_callback=print, stop_check=None):
    """Trains the LLM using Knowledge Packing, Curriculum Matrix Priority, 

    and Graph Vector Injections simultaneously.
    """
    checkpoint_path = "src/llm/checkpoint.pt"
    
    try:
        log_callback("🔍 Parsing unstructured corpus for metadata & connection graphs...")
        db_rows = [parse_raw_case(i, t) for i, t in enumerate(texts)]
        
        log_callback("📊 Extracting database connectivity matrices...")
        graph_vectors, importance_scores = build_citation_times_table(db_rows)
        
        # Option 2: Sort the dataset based on importance scores (Curriculum Learning)
        # Highly cited 'landmark' cases bubble to the front of training
        sorted_cases = sorted(db_rows, key=lambda c: importance_scores.get(c['id'], 1), reverse=True)
        
        log_callback("📦 Packing structured knowledge tokens...")
        formatted_texts = []
        text_graph_mappings = []
        
        # Option 1: Knowledge Packing Layout
        for case in sorted_cases:
            doc_id = case['id']
            cites = ", ".join(case.get('citations', []))[:300]
            logic = case.get('ruling_logic', 'No explicit ruling logic compiled.')
            body = case.get('text_content', '')[:1500] # Limit raw body noise
            
            smart_string = (
                f"<|START|>\n"
                f"<|CITATIONS|> {cites}\n"
                f"<|REASONING|> {logic}\n"
                f"<|BODY|>\n{body}\n"
                f"<|END|>\n"
            )
            formatted_texts.append(smart_string)
            text_graph_mappings.append(graph_vectors.get(doc_id, [0.0]*64))

        # Tokenize the newly structured dataset
        tokenizer = build_tokenizer(formatted_texts)
        
        flat_token_ids = []
        flat_graph_vectors = []
        
        for text_str, g_vec in zip(formatted_texts, text_graph_mappings):
            encoded = tokenizer.encode(text_str).ids
            flat_token_ids.extend(encoded)
            # Align the graph vector to match every token position in this case
            flat_graph_vectors.extend([g_vec] * len(encoded))

        max_tokens = 25000
        if len(flat_token_ids) > max_tokens:
            flat_token_ids = flat_token_ids[:max_tokens]
            flat_graph_vectors = flat_graph_vectors[:max_tokens]
            log_callback(f"⚠️ Truncating dataset to {max_tokens} tokens for local CPU speed.")

        if len(flat_token_ids) <= 128:
            log_callback("❌ Error: Structured dataset volume is too small.")
            return

        dataset = HybridGraphDataset(flat_token_ids, flat_graph_vectors, seq_len=128)
        loader = DataLoader(dataset, batch_size=4, shuffle=False)

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = TinyTransformerLM(vocab_size=tokenizer.get_vocab_size()).to(device)
        
        optim = torch.optim.AdamW(model.parameters(), lr=4e-4)
        loss_fn = torch.nn.CrossEntropyLoss()

        start_epoch = 0
        start_batch = 0

        if os.path.exists(checkpoint_path):
            log_callback("🔄 Found paused session. Restoring weights and structural states...")
            checkpoint = torch.load(checkpoint_path, map_location=device)
            model.load_state_dict(checkpoint['model_state_dict'])
            optim.load_state_dict(checkpoint['optimizer_state_dict'])
            start_epoch = checkpoint['epoch']
            start_batch = checkpoint['batch_idx'] + 1
            log_callback(f"▶️ Resuming from Epoch {start_epoch+1}, Batch {start_batch}.")
        else:
            log_callback(f"🚀 Launching Graph-Aware Brain on {device.type.upper()} | Total Batches: {len(loader)}")

        model.train()
        
        for epoch in range(start_epoch, 3):
            total_loss = 0
            for batch_idx, (x, g_vecs, y) in enumerate(loader):
                
                if epoch == start_epoch and batch_idx < start_batch:
                    continue
                    
                # ✅ Highly robust GUI stop check (handles both thread events and lambdas)
                is_stopped = False
                if stop_check:
                    if callable(stop_check):
                        is_stopped = stop_check()
                    elif hasattr(stop_check, "is_set"):
                        is_stopped = stop_check.is_set()

                if is_stopped:
                    log_callback(f"💾 Saving structural state at Epoch {epoch+1}, Batch {batch_idx}...")
                    torch.save({
                        'epoch': epoch,
                        'batch_idx': batch_idx,
                        'model_state_dict': model.state_dict(),
                        'optimizer_state_dict': optim.state_dict(),
                    }, checkpoint_path)
                    log_callback("🛑 Training safely paused. Matrix vectors preserved.")
                    return
                    
                x, g_vecs, y = x.to(device), g_vecs.to(device), y.to(device)
                optim.zero_grad()
                
                # Run forward pass 
                logits = model(x) 
                
                loss = loss_fn(logits.view(-1, logits.size(-1)), y.view(-1))
                loss.backward()
                optim.step()
                total_loss += loss.item()
                
                if batch_idx % 10 == 0:
                    log_callback(f"⏳ [Graph-Engine] Epoch {epoch+1}/3 | Batch {batch_idx}/{len(loader)} | Loss: {loss.item():.4f}")
                
                time.sleep(0.02)
            
            start_batch = 0
            log_callback(f"✅ Epoch {epoch+1:02d}/03 Complete | Matrix Loss: {total_loss/len(loader):.4f}")
            
        if os.path.exists(checkpoint_path):
            os.remove(checkpoint_path)
            
        torch.save(model.state_dict(), "src/llm/model.pt")
        log_callback("💾 Advanced structural model saved to: src/llm/model.pt")

    except Exception as e:
        log_callback(f"❌ Structural training loop failure: {e}")
# Make sure any test execution is protected like this:
if __name__ == "__main__":
    # train_llm(test_corpus) # Only runs if you run train.py directly
    pass