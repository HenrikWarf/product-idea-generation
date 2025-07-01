import json
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai.types import Tool, GenerationConfig
import vertexai
from vertexai.preview.vision_models import ImageGenerationModel
from google.cloud import storage
import uuid
from datetime import datetime, timedelta
from fastapi import HTTPException
import os
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# Configure Vertex AI for image generation
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT")
LOCATION = "us-central1"
vertexai.init(project=PROJECT_ID, location=LOCATION)

# Configure the Gemini API for text generation
model_name = 'gemini-2.5-pro'
# Use the unified client for both Vertex and GenAI services
client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

class ProductIdea(BaseModel):
    name: str
    description: str

class ProductDevelopment(BaseModel):
    name: str
    description: str
    features: list[str]
    materials: list[str]

class GenericStage(BaseModel):
    data: dict

class RegenerateRequest(BaseModel):
    ideaName: str
    instruction: str
    data: dict

async def generate_images_for_ideation(product_ideas: list, instructions: str, num_images: int = 4):
    image_model = ImageGenerationModel.from_pretrained("imagegeneration@006")
    storage_client = storage.Client()
    bucket_name = "campagin_creatives" # <--- IMPORTANT: Replace with your GCS bucket name
    
    generated_images_data = []

    for idea in product_ideas:
        prompt = (
            f"Create a photorealistic image of a fashion product based on the following idea: "
            f"Name: {idea.get('name', 'N/A')}. "
            f"Description: {idea.get('description', 'N/A')}. "
            f"Style should be clean, modern, and suitable for an e-commerce website. "
            f"Additional instructions: {instructions if instructions else 'None'}"
        )
        
        images = image_model.generate_images(
            prompt=prompt,
            number_of_images=num_images,
            aspect_ratio="1:1"
        )
        
        idea_images = []
        for i, image in enumerate(images):
            file_name = f"product-images/{uuid.uuid4()}.png"
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(file_name)
            
            # In-memory file for upload
            blob.upload_from_string(image._image_bytes, content_type="image/png")
            
            # Generate a signed URL to provide temporary access to the file.
            # This is the secure way to share files from a bucket with uniform access control.
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=1), # The URL will be valid for 1 hour
                method="GET",
            )
            
            idea_images.append({"url": signed_url, "description": f"Image {i+1} for {idea.get('name', 'N/A')}"})
        
        generated_images_data.append({
            "idea_name": idea.get('name', 'N/A'),
            "images": idea_images
        })
        
    return generated_images_data

async def process_stage(stage_name: str, full_context: dict):
    instructions = full_context.pop('instructions', '')

    if stage_name == "Image Generation":
        product_ideation_output = full_context.get("product_ideation", {})
        product_ideas = product_ideation_output.get("key_data", {}).get("product_ideas", [])

        if not product_ideas:
             return {"error": "No product ideas found from the 'Product Ideation' stage."}

        try:
            generated_images = await generate_images_for_ideation(product_ideas, instructions)
            
            response_text = "Generated product images based on the ideation stage. Each idea has four visual concepts."
            short_summary = f"Generated {len(product_ideas) * 4} images for {len(product_ideas)} product ideas."

            return {
                "response_text": response_text,
                "short_summary": short_summary,
                "key_data": { "generated_image_sets": generated_images }
            }
        except Exception as e:
            return {"error": f"Failed to generate images: {str(e)}"}

    prompt_instructions = f"""
    Based on all the information available, and paying close attention to the specific instructions, generate the output for the '{stage_name}' stage.

    Your response MUST be a valid JSON object with the following structure:
    {{
        "response_text": "A detailed, well-formatted markdown string for the stage's output. This should be a comprehensive analysis, plan, or strategy for the current stage.",
        "short_summary": "A concise, one-sentence summary of this stage's output.",
        "key_data": {{
            "data_point_1": "A critical data point or summary for this stage.",
            "data_point_2": "Another essential piece of information."
        }}
    }}
    
    For the '{stage_name}' stage, please identify 2-4 of the most critical data points to be used later in the process and put them in the key_data object. The keys for the key_data object should be descriptive and in snake_case.
    """

    if stage_name == "Product Ideation":
        prompt_instructions = f"""
        Based on all the information available, and paying close attention to the specific instructions, generate the output for the '{stage_name}' stage.

        Your response MUST be a valid JSON object with the following structure:
        {{
            "response_text": "A detailed, well-formatted markdown string describing 2-3 distinct product ideas. Each idea should have a name and a detailed description.",
            "short_summary": "A concise, one-sentence summary of the product ideas.",
            "key_data": {{
                "product_ideas": [
                    {{ "name": "Idea 1 Name", "description": "Detailed description for Idea 1." }},
                    {{ "name": "Idea 2 Name", "description": "Detailed description for Idea 2." }}
                ]
            }}
        }}
        """

    prompt = f"""
    You are an expert assistant in a fashion product development workflow.
    The current stage is: '{stage_name}'.
    The data from all previous stages is provided below in JSON format:
    {json.dumps(full_context, indent=2)}

    The user has provided the following specific instructions for this stage:
    ---
    {instructions if instructions else "No specific instructions provided."}
    ---

    {prompt_instructions}
    """
    
    try:
        response = client.models.generate_content(model=model_name, contents=prompt)

        cleaned_text = response.text.strip()
        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:-3]
        
        parsed_response = json.loads(cleaned_text)
        parsed_response['stage'] = stage_name
        parsed_response['status'] = 'completed'
        return parsed_response

    except Exception as e:
        return {"error": str(e), "raw_output": response.text if 'response' in locals() else "No response from model."}

@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.post("/api/product_ideation")
async def product_ideation(payload: GenericStage):
    return await process_stage("Product Ideation", payload.data)

@app.post("/api/image_generation")
async def image_generation(payload: GenericStage):
    return await process_stage("Image Generation", payload.data)

@app.post("/api/product_development")
async def product_development(payload: GenericStage):
    return await process_stage("Product Development", payload.data)

@app.post("/api/enrich")
async def enrich(payload: GenericStage):
    return await process_stage("Enrich", payload.data)

@app.post("/api/analysis_and_insights")
async def analysis_and_insights(payload: GenericStage):
    return await process_stage("Analysis & Insights", payload.data)

@app.post("/api/assortment_strategy")
async def assortment_strategy(payload: GenericStage):
    return await process_stage("Assortment Strategy", payload.data)

@app.post("/api/component_management")
async def component_management(payload: GenericStage):
    return await process_stage("Component Management", payload.data)

@app.post("/api/demand_planning")
async def demand_planning(payload: GenericStage):
    return await process_stage("Demand Planning", payload.data)

@app.post("/api/supply_planning")
async def supply_planning(payload: GenericStage):
    return await process_stage("Supply Planning", payload.data)

@app.post("/api/plan_build_visualize_assortment")
async def plan_build_visualize_assortment(payload: GenericStage):
    return await process_stage("Plan, Build & Visualize Assortment", payload.data)

@app.post("/api/purchase")
async def purchase(payload: GenericStage):
    return await process_stage("Purchase", payload.data)

@app.post("/api/present")
async def present(payload: GenericStage):
    return await process_stage("Present", payload.data)

@app.post("/api/produce")
async def produce(payload: GenericStage):
    return await process_stage("Produce", payload.data)

@app.post("/api/ship")
async def ship(payload: GenericStage):
    return await process_stage("Ship", payload.data)

@app.post("/api/allocate")
async def allocate(payload: GenericStage):
    return await process_stage("Allocate", payload.data)

@app.post("/api/sell")
async def sell(payload: GenericStage):
    return await process_stage("Sell", payload.data)

@app.post("/api/regenerate-images")
async def regenerate_images(payload: RegenerateRequest):
    try:
        # or we need to adjust how we retrieve it. For now, we'll create a placeholder idea.
        # A more robust solution would be to pass the full context to this endpoint.
        product_ideation_output = payload.data.get("product_ideation", {})
        product_ideas_full = product_ideation_output.get("key_data", {}).get("product_ideas", [])
        
        target_idea = next((idea for idea in product_ideas_full if idea['name'] == payload.ideaName), None)
        
        if not target_idea:
            # Fallback if the idea isn't found in the context
            target_idea = {"name": payload.ideaName, "description": "A stylish and innovative product."}

        product_idea_list = [target_idea]

        # We call the existing image generation function, but with the new instructions
        regenerated_images_data = await generate_images_for_ideation(
            product_ideas=product_idea_list, 
            instructions=payload.instruction,
            num_images=4
        )
        # The output from generate_images_for_ideation is a list containing a dict.
        # We return the content of that dict.
        return {"images": regenerated_images_data[0]}
    except Exception as e:
        # It's better to return a proper HTTP status code for errors.
        raise HTTPException(status_code=500, detail=f"Failed to regenerate images: {str(e)}")

@app.post("/api/generate_summary")
async def generate_summary(payload: GenericStage):
    prompt = f"""
    You are an expert AI assistant creating a "Digital Brief" for a new fashion product. This is a living document that will be updated as the project progresses through its stages.
    
    The data from all completed stages of the product development workflow is provided below in JSON format:
    {json.dumps(payload.data, indent=2)}

    Based on the cumulative information from all completed stages, please synthesize the data into a cohesive executive summary.
    Your summary should tell a story of the product's journey so far. As more stages are completed, the summary will become more detailed.

    The response MUST be a valid JSON object with the following structure:
    {{
      "title": "Digital Brief: [Product Name/Concept]",
      "sections": [
        {{
          "heading": "Section Title 1 (e.g., Overall Project Status & Vision)",
          "icon": "Lightbulb",
          "content": "A detailed markdown-formatted paragraph for this section. Start with a high-level overview of the project's current status."
        }},
        {{
          "heading": "Section Title 2 (e.g., Key Insights So Far)",
          "icon": "Users",
          "content": "A detailed markdown-formatted paragraph for this section, summarizing key findings."
        }}
      ]
    }}
    Generate 2-4 relevant sections for the summary based on the available data. Icon names should be simple, single words from the react-icons/fa library (e.g., Lightbulb, Users, Pallet, ChartLine, Store).
    """
    
    try:
        response = client.models.generate_content(model=model_name, contents=prompt)

        cleaned_text = response.text.strip()
        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:-3]

        return json.loads(cleaned_text)
    except Exception as e:
        return {"error": str(e), "raw_output": response.text if 'response' in locals() else "No response from model."}

@app.post("/api/generate_key_features")
async def generate_key_features(payload: GenericStage):
    """
    Analyzes the entire product brief data and extracts key, high-level features.
    """
    full_context = payload.data

    prompt = f"""
    You are an expert fashion industry analyst. Your task is to analyze the complete digital brief provided below and extract the most critical, high-level features of the collection.

    The complete brief data is as follows:
    {json.dumps(full_context, indent=2)}

    Based on this data, please identify 4-6 of the most essential concepts that define this collection. Focus on themes like the core aesthetic, target customer, key materials, standout product features, and sustainability angles.

    Your response MUST be a valid JSON object with a single key "key_features", which is an array of strings. Each string should be a concise, impactful feature description.

    Example Response Format:
    {{
        "key_features": [
            "For the 20-25 year old, style-conscious Swedish urbanite.",
            "A 'Scandinavian Sporty Chic' aesthetic blending minimalism and function.",
            "Built on a versatile layering system for the unpredictable Nordic fall.",
            "Hero pieces include a convertible technical trench and a minimalist puffer.",
            "Emphasis on sustainable materials like Recycled Nylon and Organic Cotton."
        ]
    }}
    """
    
    try:
        response = client.models.generate_content(model=model_name, contents=prompt)
        cleaned_text = response.text.strip()
        if cleaned_text.startswith("```json"):
            cleaned_text = cleaned_text[7:-3]
        
        parsed_response = json.loads(cleaned_text)
        return parsed_response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) 