import os
import typing as t

import openai
from langchain.docstore.document import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from singer_sdk import exceptions
from singer_sdk import typing as th
from singer_sdk._singerlib.messages import (
    Message,
    SchemaMessage,
)

from map_gpt_embeddings.sdk_fixes.mapper_base import BasicPassthroughMapper
from map_gpt_embeddings.sdk_fixes.messages import RecordMessage


class GPTEmbeddingMapper(BasicPassthroughMapper):
    """Split documents into segments, then vectorize."""

    name = "map-openai-embeddings"

    def map_schema_message(self, message_dict: dict) -> t.Iterable[Message]:
        for result in t.cast(
            t.Iterable[SchemaMessage], super().map_schema_message(message_dict)
        ):
            properties = result.schema["properties"]

            # Add an "embeddings" property to the schema
            properties["embeddings"] = th.ArrayType(th.NumberType).to_dict()

            # Add metadata property to the schema, if not already present
            metadata_property = self.config["document_metadata_property"]
            if metadata_property not in properties:
                properties[metadata_property] = th.ObjectType().to_dict()

            # Add segment_number to the schema and key properties
            properties["segment_number"] = th.IntegerType().to_dict()
            result.key_properties.append("segment_number")

            yield result

    config_jsonschema = th.PropertiesList(
        th.Property(
            "document_text_property",
            th.StringType,
            default="page_content",
        ),
        th.Property(
            "document_metadata_property",
            th.StringType,
            default="metadata",
        ),
        th.Property(
            "openai_api_key",
            th.StringType,
            secret=True,
            description="OpenAI API key. Optional if `OPENAI_API_KEY` env var is set.",
        ),
    ).to_dict()

    def _validate_config(
        self,
        raise_errors: bool = True,
        warnings_as_errors: bool = False,
    ) -> tuple[list[str], list[str]]:
        """Validate configuration input against the plugin configuration JSON schema.

        Args:
            raise_errors: Flag to throw an exception if any validation errors are found.
            warnings_as_errors: Flag to throw an exception if any warnings were emitted.

        Returns:
            A tuple of configuration validation warnings and errors.

        Raises:
            ConfigValidationError: If raise_errors is True and validation fails.
        """
        super()._validate_config(raise_errors, warnings_as_errors)
        if (
            raise_errors
            and self.config.get("openai_api_key", None) is None
            and "OPENAI_API_KEY" not in os.environ
        ):
            raise exceptions.ConfigValidationError(
                "Must set at least one of the following: `openai_api_key` setting, "
                f"`{self.name.upper().replace('-', '_')}_OPEN_API_KEY` env var, or "
                " `OPENAI_API_KEY` env var."
            )

    def split_record(self, record: dict) -> t.Iterable[dict]:
        """Split a record dict to zero or more record dicts.

        Args:
            record: The record object to split.

        Yields:
            A generator of record dicts.
        """
        raw_document_text = record[self.config["document_text_property"]]
        metadata_dict = record.get(self.config["document_metadata_property"], {})

        if not self.config.get("split_documents", True):
            return record

        splitter_config = self.config.get("splitter_config", {})
        if "chunk_size" not in splitter_config:
            splitter_config["chunk_size"] = 1000
        if "chunk_overlap" not in splitter_config:
            splitter_config["chunk_overlap"] = 200
        text_splitter = RecursiveCharacterTextSplitter(**splitter_config)

        document = Document(page_content=raw_document_text, metadata=metadata_dict)

        document_segments = text_splitter.split_documents([document])

        # assert document_segments and len(
        #     document_segments
        # ), "No documents output from split."
        if len(document_segments) > 1:
            self.logger.debug("Document split into %s segments", len(document_segments))
        elif len(document_segments) == 1:
            self.logger.debug("Document not split", len(document_segments))

        for i, doc_segment in enumerate(document_segments):
            new_record = record.copy()
            new_record[self.config["document_text_property"]] = doc_segment.page_content
            new_record[self.config["document_metadata_property"]] = doc_segment.metadata
            new_record["segment_number"] = i
            yield new_record

    def get_embeddings(
        self, text: str, api_key: str, model="text-embedding-ada-002"
    ) -> list[float]:
        """Gets embedding for a given text, using the OpenAPI Embeddings endpoint.

        https://platform.openai.com/docs/api-reference/embeddings/create

        Returns an object with the following shape:
        ```
        {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "embedding": [  # <-- this is the embedding we want
                        0.0023064255,
                        -0.009327292,
                        .... (1536 floats total for ada-002)
                        -0.0028842222,
                    ],
                    "index": 0,
                }
            ],
            "model": "text-embedding-ada-002",
            "usage": {
                "prompt_tokens": 8,
                "total_tokens": 8,
            }
        }
        ```

        Args:
            text: The text to create embeddings for.
            model: The model to use. Defaults to "text-embedding-ada-002".

        Returns:
            A list of floats representing the embedding. 1536 floats for ada-002.
        """
        text = text.replace("\n", " ")
        response_dict: dict = openai.Embedding.create(
            input=[text], model=model, api_key=api_key
        )
        return response_dict["data"][0]["embedding"]

    def map_record_message(self, message_dict: dict) -> t.Iterable[RecordMessage]:
        for split_record in self.split_record(message_dict["record"]):
            try:
                split_record["embeddings"] = self.get_embeddings(
                    text=split_record[self.config["document_text_property"]],
                    model="text-embedding-ada-002",
                    api_key=self.config.get("openai_api_key", None),
                )
            except openai.error.RateLimitError as ex:
                raise exceptions.AbortedSyncFailedException(
                    "Sync aborted due to OpenAI rate limit reached. Error message:\n"
                    + str(ex)
                ) from ex
            new_message = message_dict.copy()
            new_message["record"] = split_record

            yield t.cast(RecordMessage, RecordMessage.from_dict(new_message))
