# Databricks notebook source
# MAGIC %md 
# MAGIC You may find this series of notebooks at https://github.com/databricks-industry-solutions/computer-vision-foundations. For more information about this solution accelerator, visit https://www.databricks.com/blog/2021/12/17/enabling-computer-vision-applications-with-the-data-lakehouse.html.

# COMMAND ----------

# MAGIC %md The purpose of this notebook is to present a pattern for the loading of image data for model training.  

# COMMAND ----------

# DBTITLE 1,Retrieve Configurations
# MAGIC %run "./01_Configuration"

# COMMAND ----------

# DBTITLE 1,Import Required Libraries
import pyspark.sql.functions as f
from pyspark.sql.types import *

import io
from PIL import Image, ImageStat, ExifTags

# COMMAND ----------

# DBTITLE 1,Reset Checkpoint and incoming files (Optional)
# only enable the next line if you intend to rebuild the images table
dbutils.fs.rm(config['checkpoint_path'], recurse=True)
dbutils.fs.rm(config['checkpoint_path_inference'], recurse=True)
dbutils.fs.rm(config['checkpoint_path_inference_73'], recurse=True)
dbutils.fs.rm(config['incoming_image_file_path'], recurse=True)
dbutils.fs.cp(config['raw_image_file_path'], config['incoming_image_file_path'], recurse=True)

# COMMAND ----------

# DBTITLE 1,Create or recreate Database
spark.sql(f"""drop database if exists cv cascade""") # enable this line if you need to reset the database environment
spark.sql(f"""create database if not exists cv location '{config['database_root']}'""")

# COMMAND ----------

# MAGIC %md ## Introduction
# MAGIC 
# MAGIC The purpose of this series of notebooks is to demonstrate an end-to-end workflow by which we might train a model on incoming image data and deploy that model in various modes. The first step in this workflow is to process the images being generated by local, camera-enabled devices.
# MAGIC 
# MAGIC In our scenario, we've used a [Raspberry Pi 4 Model B device (4 GB RAM)](https://www.raspberrypi.com/products/raspberry-pi-4-model-b/) equipped with an [8-MP Raspberry Pi Camera Board v2](https://www.raspberrypi.com/products/camera-module-v2/) to capture images.  (For reference, the script used to generate and transmit these images is found in the appendix section of this notebook.  Any number of different devices and/or configurations could be used to capture and transmit images.) These images are sent from the local device to a cloud storage account.  Once landed in cloud storage account, we load the image data to queriable tables, extracting metadata and statistics along the way. 
# MAGIC 
# MAGIC <img src='https://brysmiwasb.blob.core.windows.net/demos/images/cv_silver_etl_03.png' width=700>

# COMMAND ----------

# MAGIC %md ## Step 1: Read Images as Stream
# MAGIC 
# MAGIC The images captured by our device are 600 x 600 pixel JPG images.  (We've used the JPG format as opposed to the lossless PNG format due to a [firmware limitation](https://picamera.readthedocs.io/en/release-1.10/api_camera.html) with the Pi Camera used in our deployment which prevents the capture of Exif metadata with formats other than JPG.)  These images are transmitted to a cloud storage location which we have mounted to our Databricks environment as a mount point path identified as */mnt/images/incoming* (based on the notebook's default configuration).  The mount point location can be backed by either an [Azure Storage container](https://docs.microsoft.com/en-us/azure/databricks/data/data-sources/azure/azure-storage#--mount-azure-blob-storage-containers-to-dbfs), [AWS S3 bucket](https://docs.databricks.com/data/data-sources/aws/amazon-s3.html#mount-aws-s3) or [Google Cloud Storage bucket](https://docs.gcp.databricks.com/data/databricks-file-system.html#mount-object-storage-to-dbfs).
# MAGIC 
# MAGIC **NOTE** You can recreate the work below by downloading these files from [this GitHub repository](https://github.com/bryansmith-db/package_images) and then placing them in a cloud storage evironment as described above. We have automated this step for you and placed the data in a temp folder */tmp/images/*
# MAGIC 
# MAGIC As the images land in the storage account, the Databricks cluster uses the [Databricks Auto Loader](https://docs.databricks.com/spark/latest/structured-streaming/auto-loader.html) functionality (aka *cloudFiles*) to recognize their arrival and immediately process them:

# COMMAND ----------

# DBTITLE 1,Read Incoming Image Files
# set processing limit for reading files
max_bytes_per_executor = 512 * 1024**2 # 512-MB limit

# define stream
incoming_images = (
  spark
    .readStream
    .format('cloudFiles')  # auto loader
    .option('cloudFiles.format', 'binaryFile') # read as binary image
    .option('recursiveFileLookup', 'true') # search subfolders (if any)
    .option('cloudFiles.includeExistingFiles', 'true') # allows complete restarts, otherwise will only read newly arrived files
    .option('pathGlobFilter', '*.jpg') # limit to JPG files
    .option('cloudFiles.maxBytesPerTrigger', sc.defaultParallelism * max_bytes_per_executor) # limit data volumes processed per cycle
    .load(config['incoming_image_file_path']) # location to read from
  )

# COMMAND ----------

# MAGIC %md The setup of our Auto Loader functionality is pretty straight-forward.  We point Spark to our cloud storage location and limit access to files with names aligned with a provided glob.  The *cloudFiles.maxBytesPerTrigger* option is intended to protect our cluster from being overwhelmed by a surge in files that could occur if a device becomes backlogged and then transmits an exceptionally large volume of files to storage in a single burst.  With our images consisting of about 0.3 megapixels and being compressed in the JPG format, each image is roughly 220 KB in size.  The *maxBytesPerTrigger* setting will allow us to process a couple thousand images at a time with each worker core.  Additional options for the configuration of Auto Loader can be found [here](https://docs.databricks.com/spark/latest/structured-streaming/auto-loader-gen2.html#common-auto-loader-options).

# COMMAND ----------

# MAGIC %md ## Step 2: Parse Data from Image Name
# MAGIC 
# MAGIC The name assigned each incoming image file captures the local date and time an image was taken as well as the ID of the device taking it.  In addition, the file name includes a pre-assigned label indicating whether or not the image includes an object of interest, *i.e.* a package placed near the front porch on which the device has been mounted.  (Typically, images would not arrive pre-labeled.  We will revisit image labeling in another series of notebooks.)
# MAGIC 
# MAGIC To extract this information, we can call a few string parsing functions as follows:

# COMMAND ----------

# DBTITLE 1,Parse Data
images_with_parsed_data = (
  incoming_images
    .withColumn('file_name', f.expr("reverse(split(path,'/'))[0]"))
    .withColumn('timestamp', f.expr("to_timestamp(split(file_name,'_')[0])"))
    .withColumn('date', f.expr('to_date(timestamp)'))
    .withColumn('device_id', f.expr("reverse(split(reverse(split(file_name,'_',2)[1]),'_',2)[1])"))
    .withColumn('label', f.expr("cast(split(reverse(split(file_name, '[.]')[0]), '_')[0] as int)"))
    )

# COMMAND ----------

# MAGIC %md ## Step 3: Extract Metadata from Images
# MAGIC 
# MAGIC Auto Loader reads each image file as a binary array and places it into the *content* field. This binary data contains not only the pixels that make up the image but also metadata captured by the local device.  This metadata, especially the [Exif data](https://en.wikipedia.org/wiki/Exif), provides information our Data Scientists may use to evaluate the data ahead of a training exercise.  We'll write a function to extract this data so that it may be presented in a more accessible, queriable format:

# COMMAND ----------

# DBTITLE 1,Define Exif Schema
exif_schema = []

# general exif tags
for t in ExifTags.TAGS:
  
  if ExifTags.TAGS[t]=='GPSInfo':
    gps_schema = []
    
    # GPSInfo tags
    for g in ExifTags.GPSTAGS:
      tag = StructField(ExifTags.GPSTAGS[g], StringType())
      if tag not in gps_schema: gps_schema += [tag]
    
    tag = StructField(ExifTags.TAGS[t], StructType(gps_schema))
    if tag not in exif_schema: exif_schema += [tag]
    
  else:
    
    tag = StructField(ExifTags.TAGS[t], StringType())
    if tag not in exif_schema: exif_schema += [tag]
    
exif_schema = StructType(exif_schema)

# COMMAND ----------

# DBTITLE 1,Define Function to Retrieve Image Metadata
def get_image_metadata_udf(image_binary):

  def _cleanse_exif(exif_with_numerical_keys):
    '''
    convert exif from dictionary with numerical keys 
    to dictionary with friendly string keys
    '''
    exif = {}
    for k, v in exif_with_numerical_keys.items():
      
      # make sure value is a string
      v = str(v)
      
      # lookup key names
      if k in ExifTags.TAGS:
        key = ExifTags.TAGS[k]
      else:
        key = k

      # if that friendly name is GPSInfo, it's value is a nested  
      # dictionary of other EXIF tags with their own key lookups
      if key=='GPSInfo':
        gps = {}
        for kg, vg in v.items():
          if kg in ExifTags.GPSTAGS:
            gps[ExifTags.GPSTAGS[kg]] = str(vg)
          else:
            gps[kg] = vg
        v = gps
        
      exif[key] = v
      
    return exif
  
  # interpret image from binary
  image = Image.open(io.BytesIO(image_binary))
    
  # extract metadata
  metadata = {}
  metadata['height'] = image.height
  metadata['width'] = image.width
  metadata['dpi_vertical'] = image.info['dpi']
  metadata['layers'] = image.layers
  metadata['mode'] = image.mode
  metadata['format'] = image.format
  metadata['exif'] = _cleanse_exif(image._getexif())

  return metadata
  #return {'test': str(metadata)}

# COMMAND ----------

# DBTITLE 1,Register Metadata Extract Function
# define schema of returned metadata
metadata_schema =  StructType([
  StructField('height', IntegerType()),
  StructField('width', IntegerType()),
  StructField('dpi', ArrayType(IntegerType())),
  StructField('layers', IntegerType()),
  StructField('mode', StringType()),
  StructField('format', StringType()),
  StructField('exif', exif_schema)
  ])

#metadata_schema = StructType([StructField('test', StringType())])

# register function for use with sql
_ = spark.udf.register('get_image_metadata', get_image_metadata_udf, metadata_schema)

# COMMAND ----------

# DBTITLE 1,Get Metadata
images_with_metadata = (
  images_with_parsed_data
    .withColumn('metadata', f.expr('get_image_metadata(content)'))
    )

# COMMAND ----------

# MAGIC %md ##Step 4: Calculate Statistics
# MAGIC 
# MAGIC In addition to metadata, we might extract various statistics from each image.  Again, we'll do this using a custom function:

# COMMAND ----------

# DBTITLE 1,Define Function to Calculate Statistics
def get_image_statistics_udf(image_binary):  

  # interpret image from binary
  image = Image.open(io.BytesIO(image_binary))

  # extract stats
  statistics = {}
  
  stat = ImageStat.Stat(image)
  statistics['mean'] = stat.mean # mean value by band/layer
  statistics['median'] = stat.median # median value by band/layer
  statistics['stddev'] = stat.stddev # stdddev by band/layer
  statistics['extrema'] = stat.extrema  # (min, max) by band/layer
  statistics['entropy'] = image.entropy() # measure of randomness to pixels
  statistics['histogram'] = image.histogram() # count of pixels by band-value

  return statistics

# define schema of returned metadata
statistics_schema =  StructType([
  StructField('mean', ArrayType(DoubleType())), 
  StructField('median', ArrayType(IntegerType())), 
  StructField('stddev', ArrayType(DoubleType())), 
  StructField('extrema', ArrayType(ArrayType(IntegerType()))), 
  StructField('entropy', DoubleType()), 
  StructField('histogram', ArrayType(IntegerType()))
  ])

# register function for use with sql
_ = spark.udf.register('get_image_statistics', get_image_statistics_udf, statistics_schema)

# COMMAND ----------

# DBTITLE 1,Get Statistics
images_with_statistics = (
  images_with_metadata
    .withColumn('statistics', f.expr('get_image_statistics(content)'))
    )

# COMMAND ----------

# MAGIC %md ## Step 5: Persist to Delta
# MAGIC 
# MAGIC With metadata and statistics extracted, we can now persist these data to a queriable table.  We'll make use of the Delta Lake format as it supports a wide range of data modification capabilities and allows us to recognize incremental changes to the data that might be useful in some downstream scenarios. This table will serve as a the focal point for model training work taking place in the next notebook:
# MAGIC 
# MAGIC **NOTE** If you wish to reset the cv.images table, please delete the files found in the checkpoint path before restarting the stream. Otherwise, the prior state of the stream ahead of the write to the cv.image table will be preserved.  These actions are performed at the top of this notebook.

# COMMAND ----------

# DBTITLE 1,Persist Data to Images Table
_ = (
  images_with_statistics
    .writeStream
    .format('delta')
    .outputMode('append')
    .option('checkpointLocation', config['checkpoint_path'])
    .trigger(once = True) # feel free to use other triggers to process continuously
    .partitionBy('date')
    .table(config['input_images_table'])
  )

# COMMAND ----------

# MAGIC %md ## Appendix: Image Capture Script
# MAGIC 
# MAGIC Earlier in this notebook, we expressed our focus was on the processing of images once they arrived in storage.  That said, we know that many folks who read this will be curious how we captured and transmitted the images to the cloud.  The script we used is provided here not to say this is the best way to perform this work in a real-world deployment but instead to provide a starting point for others designing such a routine:

# COMMAND ----------

# MAGIC %md  **TO BE RUN ON THE RASPBERRY PI DEVICE**
# MAGIC 
# MAGIC ```
# MAGIC 
# MAGIC !/usr/bin/python
# MAGIC 
# MAGIC from picamera import PiCamera
# MAGIC from time import sleep
# MAGIC import datetime as dt
# MAGIC import os
# MAGIC import boto3
# MAGIC 
# MAGIC SENSOR_ID = 3
# MAGIC 
# MAGIC # s3 bucket connection
# MAGIC access_key = '<ACCESS_KEY>'
# MAGIC s_key = '<SECRET_KEY>'
# MAGIC bucket_name = 'pb-images-iot'
# MAGIC client_s3 = boto3.client(
# MAGIC                 's3',
# MAGIC                 aws_access_key_id = access_key,
# MAGIC                 aws_secret_access_key = s_key
# MAGIC                  )
# MAGIC 
# MAGIC # configure camera
# MAGIC camera = PiCamera()
# MAGIC camera.resolution = (600, 600)
# MAGIC camera.framerate = 15
# MAGIC camera.start_preview()
# MAGIC 
# MAGIC # capture image
# MAGIC sleep(10)
# MAGIC CURRENT_DATE = dt.datetime.now().strftime('%Y-%m-%dT%H:%M:%S') + ('-%02d' % (dt.datetime.now.microsecond / 10000))
# MAGIC local_filename = '/home/pi/images/'+ CURRENT_DATE +'.jpg'
# MAGIC cloud_filename = 'incoming/'+ CURRENT_DATE + '_rpi_sensor_' + SENSOR_ID + '_front.jpg'
# MAGIC camera.capture(local_filename)
# MAGIC camera.stop_preview()
# MAGIC 
# MAGIC # upload image to s3
# MAGIC client_s3.upload_file(local_filename, bucket_name, cloud_filename)
# MAGIC 
# MAGIC # delete image from local device
# MAGIC os.remove(local_filename)
# MAGIC 
# MAGIC ```

# COMMAND ----------

# MAGIC %md The script above is oriented around an AWS deployment. With Azure, we might use a direct connection to Azure Storage through the [Azure Storage Blog library](https://pypi.org/project/azure-storage-blob/) in a manner very similar to what is shown in the sample script.  Alternatively, you might leverage the Azure IOT Hub to provide a more secure means of accessing storage as described in [this document](https://docs.microsoft.com/en-us/azure/iot-hub/iot-hub-python-python-file-upload). This particular pattern, while significantly more complex, will allow you to easily control an approved list of devices and securely transmit time-limited credentials for accessing Azure Storage.

# COMMAND ----------

# MAGIC %md In the next notebook of this series, we present two versions with different Databricks Runtimes. The use of two different Databricks runtimes is intended to align the model trained in this notebook with its deployment target.  
# MAGIC 
# MAGIC * If you intend to deploy the model as a microservice or as part of a Spark pipeline using user-defined functions, you might consider using notebook **03a**, which uses one of the latest versions of Databricks runtime.  
# MAGIC * If you intend to deploy the model to an edge device, you should use notebook **03b** to train the model on a cluster where the Python version used by the cluster is aligned with the version deployed on your device.  (Our Raspberry Pi device runs Python 3.7, and for that reason, we have trained our edge-deployed models on the Databricks 7.3 ML cluster which runs that same version of Python.) 

# COMMAND ----------

# MAGIC %md © 2021 Databricks, Inc. All rights reserved. The source in this notebook is provided subject to the Databricks License. All included or referenced third party libraries are subject to the licenses set forth below.
