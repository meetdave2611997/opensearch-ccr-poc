import random
from datetime import datetime, timedelta
import urllib3  # Fixed import
from opensearchpy import OpenSearch

# Suppress SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure the OpenSearch client
client = OpenSearch(
    hosts=[{'host': 'localhost', 'port': 9200}],
    use_ssl=False,
    verify_certs=False,
    ssl_show_warn=False
)

def generate_sample_data(index_number):
    """Generates mock telemetry data based on the index number."""
    departments = ["Engineering", "Sales", "HR", "Marketing", "Support"]
    status_codes = ["SUCCESS", "PENDING", "FAILED"]
    
    return {
        "timestamp": (datetime.utcnow() - timedelta(days=random.randint(0, 30))).isoformat(),
        "index_id": index_number,
        "department": random.choice(departments),
        "performance_score": round(random.uniform(50.0, 100.0), 2),
        "status": random.choice(status_codes),
        "active_users": random.randint(10, 500)
    }

def main():
    print("Starting data ingestion into 50 separate indices...")
    
    for i in range(1, 6):
        index_name = f"sample-idx-{i:02d}"
        document_body = generate_sample_data(i)
        
        try:
            response = client.index(
                index=index_name,
                body=document_body,
                id="doc_101", 
                refresh=True
            )
            print(f"[{i}/50] Successfully populated index: '{index_name}' | Result: {response['result']}")
            
        except Exception as e:
            print(f"Failed to write to index '{index_name}': {str(e)}")

    print("\nAll 50 indices processed successfully.")

if __name__ == "__main__":
    main()
