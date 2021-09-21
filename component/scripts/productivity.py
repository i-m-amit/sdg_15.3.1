from functools import partial 

import ee 
import json

from component import parameter as pm

ee.Initialize()

def productivity_trajectory(model, nvdi_yearly_integration, climate_yearly_integration, output):
    """
    Productivity Trend describes the trajectory of change in productivity over time. Trend is calculated by fitting a robust, non-parametric linear regression model.The significance of trajectory slopes at the P <= 0.05 level should be reported in terms of three classes:
        1) Z score < -1.96 = Potential degradation, as indicated by a significant decreasing trend,
        2) Z score > 1.96 = Potential improvement, as indicated by a significant increasing trend, or
        3) Z score > -1.96 AND < 1.96 = No significant change

In order to correct the effects of climate on productivity, climate adjusted trend analysis can be performed. There such methods are coded for the trajectory analysis. 

The following code runs the selected trend method and produce an output by reclassifying the trajectory slopes. 
    """

    # Run the selected algorithm
    trajectories = [traj['value'] for traj in pm.trajectories]
    
    # ndvi trend
    if model.trajectory == trajectories[0]:
        z_score = ndvi_trend(model.start, model.end, nvdi_yearly_integration)
    # residual trend analysis
    elif model.trajectory == trajectories[1]:
        z_score = restrend(model.start, model.end, nvdi_yearly_integration, climate_yearly_integration)
    # rain use efficiency trend
    elif model.trajectory == trajectories[3]:
        z_score = rain_use_efficiency_trend(model.start, model.end, nvdi_yearly_integration, climate_yearly_integration)

    # 1 degraded - 2 stable - 3 improved        
    trajectory = ee.Image(0) \
        .where(z_score.lt(-1.96),1) \
        .where(z_score.gt(1.96),3) \
        .where(z_score.gt(-1.96).And(z_score.lt(1.96)),2) \
        .rename('trajectory') \
        .uint8()
    
    return trajectory

def productivity_performance(aoi_model, model, nvdi_yearly_integration, climate_yearly_integration, output):
    """
    It measures local productivity relative to other similar vegetation types in similar land cover types and bioclimatic regions. It indicates how a region is performing relative to other regions with similar productivity potential.
        Steps:
        * Computation of mean NDVI for the analysis period,
        * Creation of ecologically similar regions based on USDA taxonomy and ESA CCI land cover data sets.
        * Extraction of mean NDVI for each region, creation of  a frequency distribution of this data to determine the value that represents 90th percentile,
        * Computation of the ratio of mean NDVI and max productivity (90th percentile)

    """
    
    # land cover data from esa cci
    lc = ee.Image(pm.land_cover) \
        .clip(aoi_model.feature_collection.geometry().bounds())
    lc = lc \
        .where(lc.eq(9999), pm.int_16_min) \
        .updateMask(lc.neq(pm.int_16_min))

    # global agroecological zones from IIASA
    soil_tax_usda = ee.Image(pm.soil_tax) \
        .clip(aoi_model.feature_collection.geometry().bounds())

    # compute mean ndvi for the period
    ndvi_mean = nvdi_yearly_integration \
        .select('ndvi') \
        .reduce(ee.Reducer.mean()) \
        .rename(['ndvi'])

    ################
    
    # should not be here it's a hidden parameter
    
    # Handle case of year_start that isn't included in the CCI data
    lc_year_start = min(max(model.start, pm.lc_first_year), pm.lc_last_year)
    
    #################
    
    # reclassify lc to ipcc classes
    lc_reclass = lc \
        .select(f'y{lc_year_start}') \
        .remap(pm.ESA_lc_classes, pm.reclassification_matrix)

    # create a binary mask.
    mask = ndvi_mean.neq(0)

    # define projection attributes
    #TODO: reprojection does not work for a collection that has default projection WGS84(crs:4326). Need to find a solution to get the scale information from input sensors.
    # define unit of analysis as the intersect of soil_tax_usda and land cover
    similar_ecoregions = soil_tax_usda.multiply(100).add(lc_reclass)

    # create a 2 band raster to compute 90th percentile per ecoregion (analysis restricted by mask and study area)
    ndvi_id = ndvi_mean.addBands(similar_ecoregions).updateMask(mask)

    # compute 90th percentile by unit
    percentile_90 = ndvi_id.reduceRegion(
        reducer=ee.Reducer.percentile([90]).group(
            groupField=1, 
            groupName='code'
        ),
        geometry=aoi_model.feature_collection.geometry(),
        scale=30,
        maxPixels=1e15
    )

    # Extract the cluster IDs and the 90th percentile
    groups = ee.List(percentile_90.get("groups"))
    ids = groups.map(lambda d: ee.Dictionary(d).get('code'))
    percentile = groups.map(lambda d: ee.Dictionary(d).get('p90'))

    # remap the similar ecoregion raster using their 90th percentile value
    ecoregion_perc90 = similar_ecoregions.remap(ids, percentile)

    # compute the ratio of observed ndvi to 90th for that class
    observed_ratio = ndvi_mean.divide(ecoregion_perc90)

    # create final degradation output layer (9999 is background), 2 is not
    # degreaded, 1 is degraded
    prod_performance = ee.Image(0) \
        .where(observed_ratio.gte(0.5), 2) \
        .where(observed_ratio.lte(0.5), 1) \
        .rename('performance') \
        .uint8()

    
    return prod_performance

def productivity_state(aoi_model, model, ndvi_yearly_integration, climate_int, output):
    """
    It represents the level of relative productivity in a pixel compred to a historical observations of productivity for that pixel. 
    For more, see Ivits, E., & Cherlet, M. (2016). Land productivity dynamics: towards integrated assessment of land degradation at global scales. In. Luxembourg: Joint Research Centr, https://publications.jrc.ec.europa.eu/repository/bitstream/JRC80541/lb-na-26052-en-n%20.pdf
    It alows for the detection of recent changes in primary productivity as compared to the baseline period.
    
    Steps:
        * Definition of baseline and reporting perod,
        * Computation of frequency distribution of mean NDVI for baseline period with addition of 5% at the both extremes of the distribution to alow inclusion of some, if an, missed extreme values in NDVI.
        * Creation of 10 percentile classess using the data from the frequency distribution.
        * computation of mean NDVI for baseline period, and determination of the percentile class it belongs to. Assignmentof the mean NDVI for the base line period the number corresponding to that percentile class. 
        * computation of mean NDVI for reporting period, and determination of the percentile class it belongs to. Assignmentof the mean NDVI for the reporting period the number corresponding to that percentile class. 
        * Determination of the difference in class number between the reporting and baseline period
    """    
    
    # compute min and max of annual ndvi for the baseline period
    baseline_filter = ee.Filter.rangeContains('year', model.start, model.baseline_end)
    target_filter =ee.Filter.rangeContains('year', model.target_start, model.end)
    
    baseline_ndvi_range = ndvi_yearly_integration \
        .filter(baseline_filter) \
        .select('ndvi') \
        .reduce(ee.Reducer.percentile([0, 100]))

    # convert baseline ndvi imagecollection to bands
    baseline_ndvi_collection = ndvi_yearly_integration \
        .filter(baseline_filter) \
        .select('ndvi')

    baseline_ndvi_images = ee.ImageCollection.toBands(baseline_ndvi_collection)

    # add two bands to the time series: one 5% lower than min and one 5% higher than max
    
    ###############
    
    # this var needs to have an explicit name
    baseline_ndvi_5p = (baseline_ndvi_range.select('ndvi_p100').subtract(baseline_ndvi_range.select('ndvi_p0'))).multiply(0.05)
    
    ###############
    
    baseline_ndvi_extended = baseline_ndvi_images \
        .addBands(
            baseline_ndvi_range \
            .select('ndvi_p0') \
            .subtract(baseline_ndvi_5p)
        ) \
        .addBands(
            baseline_ndvi_range \
            .select('ndvi_p100') \
            .add(baseline_ndvi_5p)
        )

    # compute percentiles of annual ndvi for the extended baseline period
    percentiles = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    baseline_ndvi_perc = baseline_ndvi_extended.reduce(ee.Reducer.percentile(percentiles))

    # compute mean ndvi for the baseline and target period period
    baseline_ndvi_mean = ndvi_yearly_integration \
        .filter(baseline_filter) \
        .select('ndvi') \
        .reduce(ee.Reducer.mean()) \
        .rename(['ndvi'])
    
    target_ndvi_mean = ndvi_yearly_integration \
        .filter(target_filter) \
        .select('ndvi') \
        .reduce(ee.Reducer.mean()) \
        .rename(['ndvi'])

    # reclassify mean ndvi for baseline period based on the percentiles
    baseline_classes = ee.Image(pm.int_16_min) \
        .where(baseline_ndvi_mean.lte(baseline_ndvi_perc.select('p10')), 1) \
        .where(baseline_ndvi_mean.gt(baseline_ndvi_perc.select('p10')), 2) \
        .where(baseline_ndvi_mean.gt(baseline_ndvi_perc.select('p20')), 3) \
        .where(baseline_ndvi_mean.gt(baseline_ndvi_perc.select('p30')), 4) \
        .where(baseline_ndvi_mean.gt(baseline_ndvi_perc.select('p40')), 5) \
        .where(baseline_ndvi_mean.gt(baseline_ndvi_perc.select('p50')), 6) \
        .where(baseline_ndvi_mean.gt(baseline_ndvi_perc.select('p60')), 7) \
        .where(baseline_ndvi_mean.gt(baseline_ndvi_perc.select('p70')), 8) \
        .where(baseline_ndvi_mean.gt(baseline_ndvi_perc.select('p80')), 9) \
        .where(baseline_ndvi_mean.gt(baseline_ndvi_perc.select('p90')), 10)

    # reclassify mean ndvi for target period based on the percentiles
    target_classes = ee.Image(pm.int_16_min) \
        .where(target_ndvi_mean.lte(baseline_ndvi_perc.select('p10')), 1) \
        .where(target_ndvi_mean.gt(baseline_ndvi_perc.select('p10')), 2) \
        .where(target_ndvi_mean.gt(baseline_ndvi_perc.select('p20')), 3) \
        .where(target_ndvi_mean.gt(baseline_ndvi_perc.select('p30')), 4) \
        .where(target_ndvi_mean.gt(baseline_ndvi_perc.select('p40')), 5) \
        .where(target_ndvi_mean.gt(baseline_ndvi_perc.select('p50')), 6) \
        .where(target_ndvi_mean.gt(baseline_ndvi_perc.select('p60')), 7) \
        .where(target_ndvi_mean.gt(baseline_ndvi_perc.select('p70')), 8) \
        .where(target_ndvi_mean.gt(baseline_ndvi_perc.select('p80')), 9) \
        .where(target_ndvi_mean.gt(baseline_ndvi_perc.select('p90')), 10)

    # difference between start and end clusters >= 2 means improvement (<= -2 
    # is degradation)
    classes_change = target_classes \
        .subtract(baseline_classes) \
        .where(
            baseline_ndvi_mean \
                .subtract(target_ndvi_mean) \
                .abs().lte(100), \
            0
        )
    
    # reclassification to get the degredation classes
    # use the bytes convention 
    # 1 degraded - 2 stable - 3 improved
    degredation = ee.Image(pm.int_16_min) \
        .where(classes_change.gte(2),3) \
        .where(classes_change.lte(-2).And(classes_change.neq(pm.int_16_min)),1) \
        .where(classes_change.lt(2).And(classes_change.gt(-2)),2) \
        .rename("state") \
        .uint8()

    return degredation

def productivity_final(trajectory, performance, state, output):
    trajectory_class = trajectory.select('trajectory')
    performance_class = performance.select('performance')
    state_class = state.select('state')

    productivity = ee.Image(0)\
        .where(trajectory_class.eq(3).And(state_class.eq(3)).And(performance_class.eq(2)),3) \
        .where(trajectory_class.eq(3).And(state_class.eq(3)).And(performance_class.eq(1)),3) \
        .where(trajectory_class.eq(3).And(state_class.eq(2)).And(performance_class.eq(2)),3) \
        .where(trajectory_class.eq(3).And(state_class.eq(2)).And(performance_class.eq(1)),3) \
        .where(trajectory_class.eq(3).And(state_class.eq(1)).And(performance_class.eq(2)),3) \
        .where(trajectory_class.eq(3).And(state_class.eq(1)).And(performance_class.eq(1)),1) \
        .where(trajectory_class.eq(2).And(state_class.eq(3)).And(performance_class.eq(2)),2) \
        .where(trajectory_class.eq(2).And(state_class.eq(3)).And(performance_class.eq(1)),2) \
        .where(trajectory_class.eq(2).And(state_class.eq(2)).And(performance_class.eq(2)),2) \
        .where(trajectory_class.eq(2).And(state_class.eq(2)).And(performance_class.eq(1)),1) \
        .where(trajectory_class.eq(2).And(state_class.eq(1)).And(performance_class.eq(2)),1) \
        .where(trajectory_class.eq(2).And(state_class.eq(1)).And(performance_class.eq(1)),1) \
        .where(trajectory_class.eq(1).And(state_class.eq(3)).And(performance_class.eq(2)),1) \
        .where(trajectory_class.eq(1).And(state_class.eq(3)).And(performance_class.eq(1)),1) \
        .where(trajectory_class.eq(1).And(state_class.eq(2)).And(performance_class.eq(2)),1) \
        .where(trajectory_class.eq(1).And(state_class.eq(2)).And(performance_class.eq(1)),1) \
        .where(trajectory_class.eq(1).And(state_class.eq(1)).And(performance_class.eq(2)),1) \
        .where(trajectory_class.eq(1).And(state_class.eq(1)).And(performance_class.eq(1)),1) \
        .rename('productivity')
    
    return productivity.uint8()

def ndvi_trend(start, end, integrated_annual_vi):
    """Calculate VI trend.
    
    Calculates the trend of temporal VI using VI data from selected satellite dataset. 
    Areas where changes are not significant
    are masked out using a Mann-Kendall tau.
    """

    # Compute Kendall statistics
    n = end-start+1
    z_coefficient = pm.z_coefficient(n)
    kendall_trend = integrated_annual_vi \
        .select('ndvi') \
        .reduce(ee.Reducer.kendallsCorrelation(),2) \
        .select('ndvi_tau')\
        .multiply(z_coefficient)

    return kendall_trend

def restrend(start, end, ndvi_yearly_integration, climate_yearly_integration):
    """
    Residual trend analysis(RESTREND) predicts NDVI based on the given rainfall.
    It uses linear regression model to predict NDVI for a given rainfall amount. 
    The residual (Predicted - Obsedved) NDVI trend is considered as productivity change that is indipendent of climatic variation. 
    For further details, check the reference: Wessels, K.J.; van den Bergh, F.; Scholes, R.J. Limits to detectability of land degradation by trend analysis of vegetation index data. Remote Sens. Environ. 2012, 125, 10–22.
   Inputs: 
    start: Start of the historical period
    end: End of the monitoring period
    ndvi_yearly_integration: ee.ImageCollection of annul NDVI series
    climate_yearly_integration: ee.ImageCollection of annul rainfall series
  Output: A tuple of two ee.Image, linear trend and Mann-Kendall trend
    """
    
    # Apply function to create image collection of ndvi and climate
    ndvi_climate_yearly_integration = ndvi_climate_merge(climate_yearly_integration, ndvi_yearly_integration, start, end)

    # Compute linear trend function to predict ndvi based on climate (independent are followed by dependent var
    linear_model_climate_ndvi = ndvi_climate_yearly_integration \
        .select(['clim', 'ndvi']) \
        .reduce(ee.Reducer.linearFit())

    def ndvi_prediction_climate(image, list):
        """predict NDVI from climate. part of p_restrend function"""
    
        ndvi = linear_model_climate_ndvi \
            .select('offset') \
            .add(
                    linear_model_climate_ndvi \
                    .select('scale') \
                    .multiply(image.select('clim'))
                    ) \
            .set({'year': image.get('year')})
    
        return ee.List(list).add(ndvi)
    # Apply function to  predict NDVI based on climate
    first = ee.List([])
    predicted_yearly_ndvi = ee.ImageCollection(ee.List(
        ndvi_climate_yearly_integration \
        .select('clim') \
        .iterate(ndvi_prediction_climate, first)
    ))

    # Apply function to compute NDVI annual residuals
    #residual_yearly_ndvi = image_collection_residuals(start, end, nvdi_yearly_integration, predicted_yearly_ndvi)
    residual_yearly_ndvi = ndvi_yearly_integration.map(partial(ndvi_residuals, modeled = predicted_yearly_ndvi))

    # Fit a linear regression to the NDVI residuals
    # Compute Kendall statistics
    n = end-start+1
    z_coefficient = pm.z_coefficient(n)
    kendall_trend = residual_yearly_ndvi.select('ndvi_res')\
            .reduce(ee.Reducer.kendallsCorrelation(),2)\
            .select('ndvi_res_tau')\
            .multiply(z_coefficient)
    
    return kendall_trend

def rain_use_efficiency_trend(start, end, ndvi_yearly_integration, climate_yearly_integration):
    """
    Calculate trend based on rain use efficiency.
    It is the ratio of ANPP(annual integral of NDVI as proxy) to annual precipitation.
    """

    # Apply function to create image collection of ndvi and climate
    ndvi_climate_yearly_integration = ndvi_climate_merge(climate_yearly_integration, ndvi_yearly_integration)
    
    # Apply function to compute ue and store as a collection
    ue_yearly_collection = ndvi_climate_yearly_integration.map(use_efficiency_ratio)


    # Compute Kendall statistics
    n = end-start+1
    z_coefficient = pm.z_coefficient(n)
    kendall_trend = ue_yearly_collection.select('ue')\
        .reduce(ee.Reducer.kendallsCorrelation(),2)\
        .select('ue_tau')\
        .multiply(z_coefficient)
    
    return kendall_trend

 
def ndvi_climate_merge(climate_yearly_integration, ndvi_yearly_integration, start=None, end=None):
    """Creat an ImageCollection of annual integral of NDVI and annual inegral of climate data"""
    
    # create the filter to use in the join
    join_filter = ee.Filter.equals(
        leftField = 'year',
        rightField = 'year'
    )
    
    join = ee.Join.inner('clim', 'ndvi', 'year')
    
    # join the 2 collections
    inner_join = join.apply(
        climate_yearly_integration.select('clim'),
        ndvi_yearly_integration.select('ndvi'),
        join_filter
    )
    
    joined = inner_join.map(lambda feature:
        ee.Image \
            .cat(feature.get('clim'), feature.get('ndvi')) \
            .set('year', ee.Image(feature.get('clim')).get('year')) # both have the same year
    )
    
    return ee.ImageCollection(joined)

def ndvi_residuals(image, modeled):
    """Function to compute residuals (ndvi obs - ndvi pred). part of p_restrend function"""
    
    # get the year from image props
    year = image.get('year')
    
    ndvi_o = image.select('ndvi')
    
    ndvi_p = modeled.filter(ee.Filter.eq('year', year)).first()
    
    ndvi_r = ee.Image.constant(year) \
        .float() \
        .addBands(ndvi_o.subtract(ndvi_p)) \
        .rename(['year', 'ndvi_res'])
        
    return ndvi_r


def use_efficiency_ratio(image):
    """Function to map over the ndvi and climate collection to get the rain use efficiency and store it as an imageCollection"""
        
    # extract climate and ndvi median values
    ndvi_img = image.select('ndvi')
    clim_img = image.select('clim').divide(1000)
    year = image.get('year')
    divide_img = ndvi_img \
        .divide(clim_img) \
        .addBands(ee.Image.constant(year).float()) \
        .rename(['ue', 'year']) \
        .set({'year': year})

    return divide_img