from PIL import Image
import os
import urllib.request
from fastapi import APIRouter
from pims.formats import FORMATS
import io
from pims.importer.importer import FileImporter

from pims.files.file import (
    EXTRACTED_DIR, HISTOGRAM_STEM, ORIGINAL_STEM, PROCESSED_DIR, Path,
    SPATIAL_STEM, UPLOAD_DIR_PREFIX, unique_name_generator
)

def getImage(path, image):
	filepath = os.path.join(path, image)
	# If image does not exist locally -> download image
	if not os.path.exists(path):
		os.mkdir("/data/pims/upload_test_png")
	
	if not os.path.exists(filepath):
		try:
			url = f"https://doc.cytomine.org/images/{image}" #OAC
			urllib.request.urlretrieve(url, filepath)
		except Exception as e:
			print("Could not download image")
			print(e)
	
	if not os.path.exists(path + "processed"):
		try:
			fi = FileImporter("/data/pims/upload_test_png/cytomine-org-logo.png")
			fi.upload_dir = "/data/pims/upload_test_png"
			fi.processed_dir = fi.upload_dir / Path("processed")
			fi.mkdir(fi.processed_dir)
		except Exception as e:
			print(path + "/processed could not be created")
			print(e)

	if not os.path.exists(path+"/processed/visualisation.PNG"):
		try:
			fi.upload_path = Path(filepath)
			original_filename = Path(f"{ORIGINAL_STEM}.PNG")
			fi.original_path = fi.processed_dir / original_filename
			fi.mksymlink(fi.original_path, fi.upload_path)
			spatial_filename = Path(f"{SPATIAL_STEM}.PNG")
			fi.spatial_path = fi.processed_dir / spatial_filename
			fi.mksymlink(fi.spatial_path, fi.original_path)
		except Exception as e:
			print("Importation of images could not be done")
			print(e)
			
def test_img_exists():
	# Test if the file exists, either locally either with the OAC
	path = "/data/pims/upload_test_png/"
	image = "cytomine-org-logo.png"
	getImage(path, image)
	assert os.path.exists(path+image) == True
	
router = APIRouter()
tile_tags = ['Tiles']

def test_info(app, client):
	# Verify if image format corresponds
	response = client.get('/image/upload_test_png/cytomine-org-logo.png/info/image')
	assert response.status_code == 200
	assert "png" in response.json()['original_format'].lower()
	
def test_get_tile(app, client):
	response = client.get("/image/upload_test_png/cytomine-org-logo.png/tile/zoom/0/ti/0", headers={"accept": "image/jpeg"})
	assert response.status_code == 200
	img_response = Image.open(io.BytesIO(response.content))
	width_resp, height_resp = img_response.size
	assert width_resp == 300
	assert height_resp == 79
	
def test_get_norm_tile(app, client):
	response = client.get("/image/upload_test_png/cytomine-org-logo.png/normalized-tile/zoom/1/ti/0", headers={"accept": "image/jpeg"})
	assert response.status_code == 200
	
	img_response = Image.open(io.BytesIO(response.content))
	width_resp, height_resp = img_response.size
	assert width_resp == 256
	assert height_resp == 79
	
def test_get_thumb(app, client):
	response = client.get("/image/upload_test_png/cytomine-org-logo.png/thumb", headers={"accept": "image/jpeg"})
	assert response.status_code == 200
	
def test_get_resized(app, client):
	response = client.get("/image/upload_test_png/cytomine-org-logo.png/resized", headers={"accept": "image/jpeg"})
	assert response.status_code == 200
	
def test_get_mask(app, client):
	response = client.post("/image/upload_test_png/cytomine-org-logo.png/annotation/mask", headers={"accept": "image/jpeg"}, json={"annotations":[{"geometry": "POINT(10 10)"}], "height":50, "width":50})
	assert response.status_code == 200
	
def test_get_crop(app, client):
	response = client.post("/image/upload_test_png/cytomine-org-logo.png/annotation/crop", headers={"accept": "image/jpeg"}, json={"annotations":[{"geometry": "POINT(10 10)"}], "height":50, "width":50})
	assert response.status_code == 200
	
"""	
def test_crop_null_annot(app, client):
	response = client.post("/image/upload_test_png/cytomine-org-logo.png/annotation/crop", headers={"accept": "image/jpeg"}, json={"annotations": [], "height":50, "width":50})
	print(response.__dict__)
	assert response.status_code == 400
"""	

def test_histogram_perimage(app, client):
	response = client.get("/image/upload_test_png/cytomine-org-logo.png/histogram/per-image", headers={"accept": "image/jpeg"})
	assert response.status_code == 200
