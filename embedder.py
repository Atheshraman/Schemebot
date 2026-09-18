from typing import List

from sentence_transformers import SentenceTransformer

from supabase_client import get_supabase_client

MODEL_NAME = "all-MiniLM-L6-v2"
BATCH_SIZE = 32


def _build_embed_text(scheme: dict) -> str:
    name = scheme.get("name") or ""
    description = scheme.get("description") or ""
    benefits = scheme.get("benefits") or ""
    return f"{name}. {description}. Benefits: {benefits}".strip()


def embed_all_schemes() -> None:
    supabase = get_supabase_client()
    model = SentenceTransformer(MODEL_NAME)

    response = supabase.table("schemes").select("*").is_("embedding", "null").execute()
    schemes: List[dict] = response.data or []

    if not schemes:
        print("No schemes without embeddings.")
        return

    texts = [_build_embed_text(scheme) for scheme in schemes]
    embeddings = model.encode(texts, batch_size=BATCH_SIZE, show_progress_bar=True)

    for index, scheme in enumerate(schemes, start=1):
        embedding = embeddings[index - 1]
        supabase.table("schemes").update({"embedding": embedding.tolist()}).eq("id", scheme["id"]).execute()

        if index % 10 == 0 or index == len(schemes):
            print(f"Embedded {index}/{len(schemes)} schemes")


if __name__ == "__main__":
    embed_all_schemes()
