from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
import boto3
from dotenv import dotenv_values
import requests
from omnipost_api.fernet import FernetEncryption

class User(AbstractUser):
    pass



class Platform(models.Model):
    """
    ## Configuration for a social media platform, eg. the details needed to connect to the platform's API.
    
    - Every platform should be configured in the following manner:
    ```python
    config = {
        "INSTANCE": {
            "key1": "value1",
            "key2": "value2",
            ...
        },
        "ACTIONS": {
            "POST_IMAGE": [
                [request1, expected_response_code1, variable_mapping1],
                [request2, expected_response_code2, variable_mapping2],
                ...
            ],
            "POST_VIDEO": [
                [request1, expected_response_code1, variable_mapping1],
                ...
            ],
            "POST_TEXT": [
                [request1, expected_response_code1, variable_mapping1],
                ...
            ],
            "POST_SHORT_FORM_VIDEO": [
                [request1, expected_response_code1, variable_mapping1],
                ...
            ],
            "POST_STORIES": [
                [request1, expected_response_code1, variable_mapping1],
                ...
            ],
        }
    }
    ```
    
    - A `request` must be of the following format: 
    {
        "base_url": "https://api.example.com",
        "endpoint": "/path/to/endpoint",
        "method": "POST",
        "headers": {
            "Authorization": "Bearer ACCESS_TOKEN",
            "Content-Type": "application/json",
            ...
        },
        "params": {
            "key1": "value1",
            "key2": "value2",
            ...
        },
        "payload": {
            "key1": "value1",
            "key2": "value2",
            ...
        }
    }
    """
    name = models.CharField(max_length=100, blank=False)
    config = models.JSONField(default=dict, blank=True, null=True)
    # The configs field would contain the configuration details needed to connect to the platform's API
    
    
    def __str__(self):
        return self.name
    
    
    
class PlatformInstance(models.Model):
    """
    An instance of a social media platform, e.g. a specific Twitter account
    """
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    credentials = models.JSONField(default=dict, blank=True, null=True)
    salt = models.BinaryField(blank=True, null=True)
    
    def save(self, password=None, *args, **kwargs):
        # Initialize credentials based on platform configs
        instance_config = self.platform.config["INSTANCE"]
        for key in instance_config.keys():
            try:
                if password:
                    if self.salt:
                        self.credentials[key] = FernetEncryption.encrypt_string(instance_config[key], password, self.salt)
                    else:
                        self.credentials[key], salt = FernetEncryption.encrypt_string(instance_config[key], password)
                        self.salt = salt
                else:
                    self.credentials[key] = self.credentials[key] # Checks if the key exists or not
            except KeyError:
                raise ValueError(f"Missing credential '{key}' for platform instance")
        super().save(*args, **kwargs)
    
    def get_credential(self, credential, password=None):
        if password is not None:
            return self.credentials[credential]
        else:
            return FernetEncryption.decrypt_string(self.credentials[credential], password, self.salt)
        
    def __str__(self):
        return f"{self.platform.name} - {self.user.username}"
        
        
        
class PostBase(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    platform_instances = models.ManyToManyField(PlatformInstance)
    created_at = models.DateTimeField(auto_now_add=True)
    post_configs = models.JSONField(default=dict, blank=True, null=True)
    
    class Meta:
        abstract = True
    
    def run_action(self, action, platform_instance: PlatformInstance) -> bool:
        """
        Execute an action on a platform instance
        
        Args:
            action (str): The action to execute (such as POST_IMAGE, POST_VIDEO, etc.)
            platform_instance (PlatformInstance): The platform instance to execute the action on
        """
        if action not in platform_instance.platform.config["ACTIONS"]:
            raise ValueError(f"Action '{action}' not defined in platform configuration")
        
        def replace_keys(request, keys):
            import json
            request_string = json.dumps(request)
            for key, value in keys.items():
                request_string = request_string.replace(f"{key}",f"{value}")
            request = json.loads(request_string)
            return request
        a = platform_instance.platform.config["ACTIONS"][action].copy()
        
        for request, expected_response_code, variable_mapping in a:
            request = replace_keys(request, platform_instance.credentials)
            request = replace_keys(request, self.post_configs[platform_instance.platform.name])
            print("Request: ", request)
            response = requests.request(
                request["method"],
                request["base_url"] + request["endpoint"],
                headers=request["headers"],
                params=request["params"],
                json=request["payload"]
            )
            # print("Response: ", response)
            if response.status_code != expected_response_code:
                raise ValueError(f"Unexpected response code: {response.status_code}. Failed to create post.")
            
            for key, value in variable_mapping.items():
                self.post_configs[platform_instance.platform.name][value] = response.json()[key]
            
            self.save()
            
        return True
        
    def run_action_on_all_platforms(self, action):
        """
        Execute an action on all platform instances
        """
        for platform_instance in self.platform_instances.all():
            self.run_action(action, platform_instance)
    
    def save_to_aws_s3(self, file_path, file_name):
        """
        Save a file to the AWS cloud for public access
        """
        s3_creds = dotenv_values(".env")

        try:
            client = boto3.client(
                's3',
                aws_access_key_id=s3_creds["AWS_ACCESS_KEY"],
                aws_secret_access_key=s3_creds["AWS_SECRET_KEY"]
            )

            client.upload_file(file_path, 'omnipost-images', file_name)
        
        except Exception as e:
            raise ValueError(f"Failed to upload file to cloud: {e}")

        return True
        
        
class PostImage(PostBase):
    """
    A normal post. text and image
    """
        
    caption = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='media/', blank=True, null=True)
    image_url = models.URLField(blank=True, null=True)
    
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        
        if self.post_configs == {}: 
            for platform_instance in self.platform_instances.all():
                self.post_configs[f"{platform_instance.platform.name}"] = {
                    "CAPTION": self.caption,
                    "IMAGE_URL": self.image_url
                }
            super().save(*args, **kwargs)
        
        if self.image and self.image_url is None:
            self.save_to_aws_s3(self.image.path, self.image.name)
            cloud_url = dotenv_values(".env")["BUCKET_URL"]
            self.image_url = f"{cloud_url}/{self.image.name}"
            for platform_instance in self.platform_instances.all():
                self.post_configs[f"{platform_instance.platform.name}"]["IMAGE_URL"] = self.image_url
            super().save()
            
class PostVideo(PostBase):
    pass

class ShortFormVideo(PostBase):
    """
    A short video, like reels, for any platform in general
    """
    pass

class Stories(PostBase):
    """
    Stories/status for any platform in general
    """
    pass


class Docs(models.Model):
    """
    Documents for any platform in general
    """
    remote_doc = models.URLField(blank=True, null=True)
    custom_doc = models.TextField(blank=True, null=True)
    platform = models.ForeignKey(Platform, on_delete=models.CASCADE, blank=True, null=True)
    platform_instance = models.ForeignKey(PlatformInstance, on_delete=models.CASCADE, blank=True, null=True)
    
    
class Notifications(models.Model):
    """
    Any notifications from the platform
    """
    pass