import sqlite3
import json
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

# 1. Load a fast, local embedding model (downloads automatically the first time)
print("📥 Loading local tokenization model...")
model = SentenceTransformer('all-MiniLM-L6-v2') 

# 2. Connect to your database
db_path = r"C:\LegalSorter\index\legal_sorter.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 3. Pull the text AND the metadata (citations, keywords, entities)
print("📂 Reading database...")
cursor.execute("""
    SELECT id, text, citations_json, keywords_json, entities_json 
    FROM documents 
    WHERE text IS NOT NULL AND text != ''
""")
rows = cursor.fetchall()

# 4. Prepare the data
document_ids = []
combined_texts = []

print("🧩 Fusing text, logic, and citations...")
for row in rows:
    doc_id = row[0]
    main_text = row[1]
    
    # Safely load JSON metadata if it exists
    citations = json.loads(row[2]) if row[2] else []
    keywords = json.loads(row[3]) if row[3] else []
    entities = json.loads(row[4]) if row[4] else []
    
    # Create a "Searchable Super-String"
    # This ensures the AI tokenizes the case logic and citations right alongside the text
    searchable_content = f"""
    CITATIONS: {', '.join(citations)}
    KEYWORDS: {', '.join(keywords)}
    ENTITIES: {', '.join(entities)}
    CASE TEXT: {main_text}
    """
    
    document_ids.append(doc_id)
    combined_texts.append(searchable_content)

# 5. Translate everything into Tokens (Embeddings)
print(f"🧠 Tokenizing {len(combined_texts)} documents (This runs locally on your CPU)...")
# This creates a matrix of numbers representing the concepts in your cases
embeddings = model.encode(combined_texts, show_progress_bar=True)

# 6. Store in a FAISS Vector Database
print("💾 Building local vector index...")
dimension_size = embeddings.shape[1] # MiniLM uses 384 dimensions
index = faiss.IndexFlatL2(dimension_size) 
index.add(np.array(embeddings).astype('float32'))

# Save the index to your laptop
faiss.write_index(index, "legal_sorter_vectors.index")

# Save the ID mapping so we know which vector belongs to which SQLite row
with open("vector_mapping.json", "w") as f:
    json.dump(document_ids, f)

print("✅ Success! Local vector database created.")
conn.close()