#!/usr/bin/env python3
# Copyright Hewlett Packard Enterprise Development LP.
"""Apply reviewed compatibility fixes to the pinned Open WebUI image."""

from pathlib import Path


CHROMA_ADAPTER = Path(
    "/app/backend/open_webui/retrieval/vector/dbs/chroma.py"
)
RETRIEVAL_UTILS = Path("/app/backend/open_webui/retrieval/utils.py")

ORIGINAL = """\
    def get(self, collection_name: str) -> Optional[GetResult]:
        # Get all the items in the collection.
        collection = self.client.get_collection(name=collection_name)
        if collection:
            result = collection.get()
            return GetResult(
                **{
                    'ids': [result['ids']],
                    'documents': [result['documents']],
                    'metadatas': [result['metadatas']],
                }
            )
        return None
"""

PATCHED = """\
    def get(self, collection_name: str) -> Optional[GetResult]:
        # Get all items without exceeding SQLite's bound-variable limit.
        # Hybrid BM25 needs the complete collection, while a single Chroma
        # get() fails on large corpora such as the SST source knowledge base.
        collection = self.client.get_collection(name=collection_name)
        if collection:
            batch_size = 500
            offset = 0
            ids = []
            documents = []
            metadatas = []
            while True:
                result = collection.get(limit=batch_size, offset=offset)
                batch_ids = result['ids']
                if not batch_ids:
                    break
                ids.extend(batch_ids)
                documents.extend(result['documents'])
                metadatas.extend(result['metadatas'])
                offset += len(batch_ids)
                if len(batch_ids) < batch_size:
                    break
            return GetResult(
                **{
                    'ids': [ids],
                    'documents': [documents],
                    'metadatas': [metadatas],
                }
            )
        return None
"""

RETRIEVAL_ORIGINAL = """\
                doc_hash = hashlib.sha256(document.encode()).hexdigest()  # Compute a hash for uniqueness
"""

RETRIEVAL_PATCHED = """\
                # Raw vector text and metadata-enriched BM25 text represent
                # the same chunk. Use the stable hash assigned before hybrid
                # retrieval so multi-query merging does not keep both copies.
                doc_hash = metadata.get(CHUNK_HASH_KEY) or hashlib.sha256(document.encode()).hexdigest()
"""


def apply_patch(target: Path = CHROMA_ADAPTER) -> bool:
    """Apply the reviewed source replacement.

    Returns True when the file changed and False when it was already patched.
    """
    source = target.read_text()
    if PATCHED in source:
        print("Open WebUI Chroma pagination patch already applied")
        return False
    if source.count(ORIGINAL) != 1:
        raise SystemExit(
            "Pinned Open WebUI Chroma adapter changed; refusing an "
            "unreviewed patch"
        )
    target.write_text(source.replace(ORIGINAL, PATCHED, 1))
    print("Applied Open WebUI Chroma pagination patch")
    return True


def apply_retrieval_patch(target: Path = RETRIEVAL_UTILS) -> bool:
    """Deduplicate hybrid multi-query results by their original chunk hash."""
    source = target.read_text()
    if RETRIEVAL_PATCHED in source:
        print("Open WebUI hybrid retrieval patch already applied")
        return False
    if source.count(RETRIEVAL_ORIGINAL) != 1:
        raise SystemExit(
            "Pinned Open WebUI retrieval utility changed; refusing an "
            "unreviewed patch"
        )
    target.write_text(
        source.replace(RETRIEVAL_ORIGINAL, RETRIEVAL_PATCHED, 1)
    )
    print("Applied Open WebUI hybrid retrieval patch")
    return True


def main() -> None:
    apply_patch()
    apply_retrieval_patch()


if __name__ == "__main__":
    main()
