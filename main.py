import uvicorn
from fastapi import FastAPI, File, UploadFile, HTTPException
from pydantic import BaseModel
from typing import Any, Optional
from datetime import datetime
import time
import os
import logging

from utils import check_required_env_vars
from azure_utils import delete_existing_pdfs, create_search_resources, reset_and_run_indexer, get_indexer_status
from config import app

# Define standardized response models
class StandardResponse(BaseModel):
    success: bool
    message: str
    data: Optional[Any] = None
    timestamp: str

# Azure configuration from environment variables (without default values exposing credentials)
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.environ.get("CONTAINER_NAME")
AZURE_SEARCH_ENDPOINT = os.environ.get("AZURE_SEARCH_ENDPOINT")
AZURE_SEARCH_KEY = os.environ.get("AZURE_SEARCH_KEY")
INDEX_NAME = os.environ.get("INDEX_NAME", "azureblob-index")
DATASOURCE_NAME = os.environ.get("DATASOURCE_NAME", "simplerag")
INDEXER_NAME = os.environ.get("INDEXER_NAME", "azureblob-indexer")

# Azure OpenAI configuration
AZURE_OPENAI_API_ENDPOINT = os.environ.get("AZURE_OPENAI_API_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_MODEL_NAME = os.environ.get("AZURE_OPENAI_MODEL_NAME")

# Check for required environment variables
required_env_vars = {
    "AZURE_STORAGE_CONNECTION_STRING": AZURE_STORAGE_CONNECTION_STRING,
    "AZURE_SEARCH_ENDPOINT": AZURE_SEARCH_ENDPOINT,
    "AZURE_SEARCH_KEY": AZURE_SEARCH_KEY,
    "AZURE_OPENAI_API_ENDPOINT": AZURE_OPENAI_API_ENDPOINT,
    "AZURE_OPENAI_API_KEY": AZURE_OPENAI_API_KEY
}

missing_vars = check_required_env_vars(required_env_vars)

@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    try:
        # Validate required environment variables
        if not AZURE_STORAGE_CONNECTION_STRING:
            return StandardResponse(
                success=False,
                message="Missing Azure Storage connection string. Please configure environment variables.",
                timestamp=datetime.now().isoformat()
            )

        # 기존 PDF 삭제
        blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service.get_container_client(CONTAINER_NAME)

        # 컨테이너 존재 확인 및 생성
        try:
            container_client.get_container_properties()
        except:
            blob_service.create_container(CONTAINER_NAME)
            container_client = blob_service.get_container_client(CONTAINER_NAME)

        # Delete existing blobs
        blobs = container_client.list_blobs()
        for blob in blobs:
            container_client.delete_blob(blob.name)

        # 새 PDF 업로드
        blob_client = blob_service.get_blob_client(container=CONTAINER_NAME, blob=file.filename)
        content = await file.read()
        blob_client.upload_blob(content, overwrite=True)

        # Explicitly reset and run indexer
        try:
            # Always create search resources first
            create_result = create_search_resources()
            print(f"Created search resources, result: {create_result}")
            
            if not create_result:
                return StandardResponse(
                    success=False,
                    message="Failed to create search resources",
                    timestamp=datetime.now().isoformat()
                )
            
            # Add a small delay to ensure Azure services register the resources
            time.sleep(3)
            
            # Verify indexer exists before attempting operations
            indexer_client = SearchIndexerClient(
                endpoint=AZURE_SEARCH_ENDPOINT,
                credential=AzureKeyCredential(AZURE_SEARCH_KEY)
            )
            
            # Implement retry logic for getting the indexer
            max_retries = 3
            retry_count = 0
            indexer_status = "unknown"
            
            while retry_count < max_retries:
                try:
                    indexer = indexer_client.get_indexer(INDEXER_NAME)
                    if indexer:
                        print(f"Indexer {INDEXER_NAME} exists, resetting and running")
                        # Reset and run the indexer
                        indexer_client.reset_indexer(INDEXER_NAME)
                        indexer_client.run_indexer(INDEXER_NAME)
                        indexer_status = "running"
                        break
                    else:
                        indexer_status = "error: Indexer exists but is invalid"
                        retry_count += 1
                        time.sleep(2)  # Wait before retrying
                except ResourceNotFoundError:
                    print(f"Retry {retry_count+1}: Indexer {INDEXER_NAME} not found, waiting...")
                    retry_count += 1
                    
                    if retry_count >= max_retries:
                        indexer_status = f"error: Indexer {INDEXER_NAME} not found after {max_retries} attempts"
                    else:
                        time.sleep(3)  # Wait before retrying
                except Exception as e:
                    print(f"Error with indexer operations: {str(e)}")
                    indexer_status = f"error: {str(e)}"
                    break
                
        except Exception as e:
            print(f"Error during indexer setup: {str(e)}")
            indexer_status = f"error: {str(e)}"
            
        return StandardResponse(
            success=True,
            message="파일 업로드 완료",
            data={"filename": file.filename, "indexer_status": indexer_status},
            timestamp=datetime.now().isoformat()
        )
    except Exception as e:
        logger.error(f"Upload error: {str(e)}")
        return StandardResponse(
            success=False,
            message=f"Upload failed: {str(e)}",
            timestamp=datetime.now().isoformat()
        )

@app.get("/indexer-status")
async def indexer_status():
    try:
        # Check that required environment variables are set and are strings
        if not AZURE_SEARCH_ENDPOINT or not isinstance(AZURE_SEARCH_ENDPOINT, str):
            return StandardResponse(
                success=False,
                message="AZURE_SEARCH_ENDPOINT not properly configured",
                data={"indexing_status": "error: configuration_missing"},
                timestamp=datetime.now().isoformat()
            )
            
        if not AZURE_SEARCH_KEY or not isinstance(AZURE_SEARCH_KEY, str):
            return StandardResponse(
                success=False,
                message="AZURE_SEARCH_KEY not properly configured",
                data={"indexing_status": "error: configuration_missing"},
                timestamp=datetime.now().isoformat()
            )
        
        status = get_indexer_status()
        
        message = "인덱싱 상태 확인 완료"
        if status == "not_found":
            message = "인덱서가 존재하지 않습니다. PDF를 업로드하여 인덱서를 생성하세요."
        
        return StandardResponse(
            success=True,
            message=message,
            data={"indexing_status": status},
            timestamp=datetime.now().isoformat()
        )
    except Exception as e:
        logger.error(f"Error in indexer_status endpoint: {str(e)}")
        return StandardResponse(
            success=False,
            message=f"Failed to get indexer status: {str(e)}",
            timestamp=datetime.now().isoformat()
        )

@app.get("/upload-status")
async def check_any_files():
    """Check if any file is already in the container."""
    try:
        blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        container_client = blob_service.get_container_client(CONTAINER_NAME)
        
        # Verify container exists
        try:
            container_client.get_container_properties()
        except:
            return StandardResponse(
                success=False,
                message="Container not found. Please upload a file first.",
                timestamp=datetime.now().isoformat()
            )
        
        # List all blobs
        blobs = list(container_client.list_blobs())
        if blobs:
            filenames = [blob.name for blob in blobs]
            return StandardResponse(
                success=True,
                message="Files found.",
                data={"filenames": filenames},
                timestamp=datetime.now().isoformat()
            )
        else:
            return StandardResponse(
                success=False,
                message="No files found in the container.",
                timestamp=datetime.now().isoformat()
            )
    except Exception as e:
        logger.error(f"Check file status error: {str(e)}")
        return StandardResponse(
            success=False,
            message=f"Error checking file status: {str(e)}",
            timestamp=datetime.now().isoformat()
        )

def search_pdf_content(query: str, top_k=3) -> dict:
    """Query Azure Search index for relevant PDF content and return structured results."""
    try:
        search_client = SearchClient(
            endpoint=AZURE_SEARCH_ENDPOINT,
            index_name=INDEX_NAME,
            credential=AzureKeyCredential(AZURE_SEARCH_KEY)
        )
        
        results = search_client.search(
            search_text=query,
            select=["content", "metadata_storage_name", "metadata_storage_path"],
            top=top_k
        )
        
        documents = []
        for result in results:
            filename = result.get("metadata_storage_name", "Unknown document")
            content = result.get("content", "")
            path = result.get("metadata_storage_path", "")
            if content:
                # Limit content length to avoid overly long prompts
                content_snippet = content[:2000] + ("..." if len(content) > 2000 else "")
                documents.append({
                    "filename": filename,
                    "content": content_snippet,
                    "path": path
                })
        
        return {
            "documents": documents,
            "count": len(documents),
            "query": query
        }
            
    except Exception as e:
        print(f"Search error: {str(e)}")
        return {"error": str(e), "documents": [], "count": 0, "query": query}

async def get_pdf_content(query="*") -> dict:
    """Retrieve all uploaded PDF content from the Azure Search index."""
    try:
        search_client = SearchClient(
            endpoint=AZURE_SEARCH_ENDPOINT,
            index_name=INDEX_NAME,
            credential=AzureKeyCredential(AZURE_SEARCH_KEY)
        )
        
        # Use "*" as a wildcard query to get all documents
        results = search_client.search(
            search_text=query,
            select=["content", "metadata_storage_name", "metadata_storage_path"],
            top=100  # Adjust based on expected document count
        )
        
        documents = []
        for result in results:
            filename = result.get("metadata_storage_name", "Unknown document")
            content = result.get("content", "")
            path = result.get("metadata_storage_path", "")
            if content:
                documents.append({
                    "filename": filename,
                    "content": content,
                    "path": path
                })
        
        return {
            "documents": documents,
            "count": len(documents)
        }
            
    except Exception as e:
        print(f"Error retrieving PDF content: {str(e)}")
        return {"error": str(e), "documents": [], "count": 0}

@app.get("/pdf-content")
async def get_pdf_content_endpoint(query: str):
    """Endpoint to display raw PDF content retrieved from Azure Search."""
    try:
        status = get_indexer_status()
        
        if status == "not_found":
            return StandardResponse(
                success=False,
                message="먼저 PDF를 업로드하여 인덱서를 생성하세요.",
                data={"status": "not_found"},
                timestamp=datetime.now().isoformat()
            )
        
        if isinstance(status, str) and status.startswith("error"):
            return StandardResponse(
                success=False,
                message=f"인덱서 상태 확인 중 오류 발생: {status}",
                data={"status": "error"},
                timestamp=datetime.now().isoformat()
            )
        
        if status != "success":
            return StandardResponse(
                success=False,
                message="파일이 아직 인덱싱 중입니다. 잠시 후 다시 시도하세요.",
                data={"status": status},
                timestamp=datetime.now().isoformat()
            )

        search_results = search_pdf_content(query)
        return StandardResponse(
            success=True,
            message="PDF 콘텐츠 검색 완료",
            data=search_results,
            timestamp=datetime.now().isoformat()
        )
    except Exception as e:
        return StandardResponse(
            success=False,
            message=f"Failed to retrieve PDF content: {str(e)}",
            timestamp=datetime.now().isoformat()
        )

@app.post("/chat")
async def chat(prompt: str):
    try:
        # Validate required environment variables
        if not all([AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_OPENAI_API_ENDPOINT, AZURE_OPENAI_API_KEY]):
            return StandardResponse(
                success=False,
                message="Missing required Azure credentials. Please configure environment variables.",
                timestamp=datetime.now().isoformat()
            )

        # 인덱싱 상태 확인
        status = get_indexer_status()
        
        if status == "not_found":
            return StandardResponse(
                success=False,
                message="먼저 PDF를 업로드하여 인덱서를 생성하세요.",
                data={"status": "not_found"},
                timestamp=datetime.now().isoformat()
            )
        
        if isinstance(status, str) and status.startswith("error"):
            return StandardResponse(
                success=False,
                message=f"인덱서 상태 확인 중 오류 발생: {status}",
                data={"status": "error"},
                timestamp=datetime.now().isoformat()
            )
        
        if status != "success":
            return StandardResponse(
                success=False,
                message="파일이 아직 인덱싱 중입니다. 잠시 후 다시 시도하세요.",
                data={"status": status},
                timestamp=datetime.now().isoformat()
            )

        # Just retrieve all PDF content without searching - properly await the coroutine
        pdf_results = await get_pdf_content("*")
        
        # Format the content for inclusion in the prompt
        pdf_context = ""
        sources = []
        
        if pdf_results["count"] > 0:
            for i, doc in enumerate(pdf_results["documents"]):
                pdf_context += f"Document {i+1}: {doc['filename']}\n"
                pdf_context += f"Content: {doc['content']}\n\n"
                sources.append({"filename": doc['filename'], "path": doc['path']})
        else:
            pdf_context = "No uploaded PDF content found."

        # Create system message with the PDF content
        system_message = """You are an AI assistant that helps answer questions based on PDF documents.
Answer based ONLY on the content in the documents provided below. and check liskt will be provided
If the information isn't in the documents, clearly state that.

Here is the content from the uploaded PDF documents:

"""
        system_message += pdf_context

        # Fix the OpenAI client initialization
        try:
            # Create the OpenAI client directly using the imported class
            client = AzureOpenAI(
                api_key=AZURE_OPENAI_API_KEY,
                api_version=AZURE_OPENAI_API_VERSION,
                azure_endpoint=AZURE_OPENAI_API_ENDPOINT
                # Removed any 'proxies' argument here
            )
           
            
            response =  client.chat.completions.create(
                model=AZURE_OPENAI_MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                top_p=0.95
            )
            
            answer = response.choices[0].message.content
            
            # Return the answer and sources
            return StandardResponse(
                success=True,
                message="챗봇 응답 생성 완료",
                data={
                    "answer": answer, 
                    "sources": sources
                },
                timestamp=datetime.now().isoformat()
            )
        except Exception as e:
            logger.error(f"OpenAI API error: {str(e)}")
            return StandardResponse(
                success=False,
                message=f"OpenAI API error: {str(e)}",
                timestamp=datetime.now().isoformat()
            )
            
    except Exception as e:
        logger.error(f"Chat error: {str(e)}")
        return StandardResponse(
            success=False,
            message=f"Chat failed: {str(e)}",
            timestamp=datetime.now().isoformat()
        )

if __name__ == "__main__":
    # Get port from environment variable or use default
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "localhost")
    
    logger.info(f"Starting FastAPI server on {host}:{port}...")
    logger.info(f"Visit http://{host}:{port}/docs for API documentation")
  
    uvicorn.run(app, host=host, port=port)
