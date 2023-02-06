import argparse
import csv
import logging
import os
from pyspark.sql import SparkSession
import pyspark.sql.functions as F
from pyspark.sql.types import DoubleType, TimestampType
import scripts.services.HDFSManager

BASE_PATH = os.path.join("/", "opt", "ml")
PROCESSING_PATH = os.path.join(BASE_PATH, "processing")
PROCESSING_PATH_INPUT = os.path.join(PROCESSING_PATH, "input")
PROCESSING_PATH_OUTPUT = os.path.join(PROCESSING_PATH, "output")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

## Spark Initializer
#
spark = SparkSession.builder \
    .config("spark.sql.legacy.timeParserPolicy", "CORRECTED") \
    .getOrCreate()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--copy_hdfs", type=str, default="0")
    parser.add_argument("--bucket_name", type=str, help="s3 input bucket")
    parser.add_argument("--processing_input_files_path", type=str, help="s3 input key prefix")
    parser.add_argument("--processing_output_files_path", type=str, help="s3 output bucket")
    args = parser.parse_args()

    logger.info("Arguments: {}".format(args))
    
    hdfs_manager = scripts.services.HDFSManager.HDFSManager(spark)
    
    ## Move ProcessingInputs to HDFS
    if args.copy_hdfs == "1":
        hdfs_manager.copy_full_to_hdfs(PROCESSING_PATH_INPUT)
    
    if args.copy_hdfs == "0":
        df_e = spark.read.csv(
            f"s3://{args.bucket_name}/{args.processing_input_files_path}/energy_dataset.csv",
            header=True
        )
    else:
        df_e = hdfs_manager.load_df(spark, "energy_dataset.csv")
    
    columns_to_drop = [
        'generation fossil coal-derived gas',
        'generation fossil oil shale',
        'generation fossil peat',
        'generation geothermal',
        'generation hydro pumped storage aggregated',
        'generation marine',
        'generation wind offshore',
        'forecast wind offshore eday ahead',
        'total load forecast',
        'forecast solar day ahead',
        'forecast wind onshore day ahead'
    ]

    df_e = df_e.drop(*columns_to_drop)
    
    for column in df_e.schema.names:
        if column != "time":
            df_e = df_e.withColumn(column, df_e[column].cast(DoubleType()))
            df_e = df_e.withColumn(column, F.round(F.col(column), 2))
                
    df_e \
        .select([F.count(F.when(F.isnan(c) | F.col(c).isNull(), c)).alias(c) for c in df_e.schema.names if c != "time"]) \
        .toPandas()
    
    def interpolate(pdf):
        pdf = pdf.set_index('time')
        pdf.interpolate(method='linear', limit_direction='forward', inplace=True, axis=0)
        pdf.reset_index(inplace=True)
        return pdf
    
    df_e_p = df_e.toPandas()
    df_e_p = interpolate(df_e_p)
    df_e = spark.createDataFrame(df_e_p)
    
    logger.info("Shape: ({},{})".format(df_e.count(), len(df_e.columns)))
        
    if args.copy_hdfs == "1":
        df_w = hdfs_manager.load_df(spark, "weather_features.csv")
    else:
        df_w = spark.read.csv(
            f"s3://{args.bucket_name}/{args.processing_input_files_path}/weather_features.csv",
            header=True
        )
        
    columns = ["city_name", "weather_id", "weather_main", "weather_description", "weather_icon", "dt_iso"]

    for c in df_w.columns:
        if c not in columns:
            df_w = df_w.withColumn(c, df_w[c].cast(DoubleType()))
            
    df_w = df_w.withColumn("time", F.to_timestamp("dt_iso", "yyyy-MM-dd HH:mm:ssVV"))
    
    df_w = df_w.drop("dt_iso")
    
    ## Remove duplicates
    
    df_w.distinct().groupby("city_name").count().show()
    
    df_w = df_w.orderBy("time").coalesce(1).dropDuplicates(subset = ["city_name", "time"])
    
    df_w.distinct().groupby("city_name").count().show()
    
    df_w_barcelona = df_w.filter(F.col("city_name") == " Barcelona")
    df_w_bilbao = df_w.filter(F.col("city_name") == "Bilbao")
    df_w_madrid = df_w.filter(F.col("city_name") == "Madrid")
    df_w_seville = df_w.filter(F.col("city_name") == "Seville")
    df_w_valencia = df_w.filter(F.col("city_name") == "Valencia")

    df_w_barcelona = df_w_barcelona.select([F.col(c).alias(c + "_barcelona") for c in df_w_barcelona.columns]).drop("city_name_barcelona")
    df_w_bilbao = df_w_bilbao.select([F.col(c).alias(c + "_bilbao") for c in df_w_bilbao.columns]).drop("city_name_bilbao")
    df_w_madrid = df_w_madrid.select([F.col(c).alias(c + "_madrid") for c in df_w_madrid.columns]).drop("city_name_madrid")
    df_w_seville = df_w_seville.select([F.col(c).alias(c + "_seville") for c in df_w_seville.columns]).drop("city_name_seville")
    df_w_valencia = df_w_valencia.select([F.col(c).alias(c + "_valencia") for c in df_w_valencia.columns]).drop("city_name_valencia")
    
    
    logger.info("Join energy_dataset_df with weather_features_df")
    
    df_e = df_e.join(df_w_barcelona, df_e.time == df_w_barcelona.time_barcelona, how='full').drop("time_barcelona")
    df_e = df_e.join(df_w_bilbao, df_e.time == df_w_bilbao.time_bilbao, how='full').drop("time_bilbao")
    df_e = df_e.join(df_w_madrid, df_e.time == df_w_madrid.time_madrid, how='full').drop("time_madrid")
    df_e = df_e.join(df_w_seville, df_e.time == df_w_seville.time_seville, how='full').drop("time_seville")
    df_e = df_e.join(df_w_valencia, df_e.time == df_w_valencia.time_valencia, how='full').drop("time_valencia")
    
    logger.info("Writing output file {} to {}".format("energy_full.csv", f"s3://{args.bucket_name}/{args.processing_output_files_path}"))
    
    if args.copy_hdfs == "1":
        ## Save DataFrame in HDFS
        hdfs_manager.save_df(df_e, "energy_full")
        hdfs_manager.copy_from_hdfs(PROCESSING_PATH_OUTPUT, "energy_full")
    else:
        ## Or save directly on S3
        df_e.repartition(1).write \
            .format("com.databricks.spark.csv") \
            .mode('overwrite') \
            .option("quote", '"') \
            .option("header", True) \
            .option("sep", ",") \
            .option('encoding', 'UTF-8') \
            .save(f"s3://{args.bucket_name}/{args.processing_output_files_path}/energy_full.csv")